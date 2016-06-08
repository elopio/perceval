# -*- coding: utf-8 -*-
#
# Copyright (C) 2015-2016 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#     Santiago Dueñas <sduenas@bitergia.com>
#     Alvaro del Castillo San Felix <acs@bitergia.com>
#

import json
import logging
import os.path

import requests

from ..backend import Backend, BackendCommand, metadata
from ..cache import Cache
from ..errors import BackendError, CacheError
from ..utils import (DEFAULT_DATETIME,
                     datetime_to_utc,
                     str_to_datetime,
                     urljoin)


logger = logging.getLogger(__name__)


MAX_BUGS = 500 # Maximum number of bugs per query


class BugzillaREST(Backend):
    """Bugzilla backend that uses its API REST.

    This class allows the fetch the bugs stored in Bugzilla
    server (version 5.0 or later). To initialize this class
    the URL of the server must be provided.

    :param url: Bugzilla server URL
    :param user: Bugzilla user
    :param password: Bugzilla user password
    :param api_token: Bugzilla token
    :param max_bugs: maximum number of bugs requested on the same query
    :param cache: cache object to store raw data
    :param origin: identifier of the repository; when `None` or an
        empty string are given, it will be set to `url` value
    """
    version = '0.1.0'

    def __init__(self, url, user=None, password=None, api_token=None,
                 max_bugs=MAX_BUGS, cache=None, origin=None):
        origin = origin if origin else url

        super().__init__(origin, cache=cache)
        self.url = url
        self.max_bugs = max(1, max_bugs)
        self.client = BugzillaRESTClient(url, user=user, password=password,
                                         api_token=api_token)

    @metadata
    def fetch(self, from_date=DEFAULT_DATETIME):
        """Fetch the bugs from the repository.

        The method retrieves, from a Bugzilla repository, the bugs
        updated since the given date.

        :param from_date: obtain bugs updated since this date

        :returns: a generator of bugs
        """
        if not from_date:
            from_date = DEFAULT_DATETIME

        logger.info("Looking for bugs: '%s' updated from '%s'",
                    self.url, str(from_date))

        self._purge_cache_queue()

        nbugs = 0
        for bug in self.__fetch_bugs(from_date):
            nbugs += 1
            yield bug

        logger.info("Fetch process completed: %s bugs fetched", nbugs)

    @metadata
    def fetch_from_cache(self):
        """Fetch bugs from the cache.

        :returns: a generator of bugs

        :raises CacheError: raised when an error occurs accessing the
            cache
        """
        if not self.cache:
            raise CacheError(cause="cache instance was not provided")

        logger.info("Retrieving cached bugs: '%s'", self.url)
        cache_items = self.cache.retrieve()
        nbugs = 0

        while True:
            try:
                raw_bugs = next(cache_items)
            except StopIteration:
                break

            bugs = json.loads(raw_bugs)

            for bug in bugs['bugs']:
                bug_id = bug['id']
                cd = json.loads(next(cache_items))
                hd = json.loads(next(cache_items))
                ad = json.loads(next(cache_items))

                bug['comments'] = cd['bugs'][str(bug_id)]['comments']
                bug['history'] = hd['bugs'][0]['history']
                bug['attachments'] = ad['bugs'][str(bug_id)]

                nbugs += 1
                yield bug

        logger.info("Retrieval process completed: %s bugs retrieved from cache",
                    nbugs)

    def __fetch_bugs(self, from_date):
        offset = 0

        while True:
            logger.debug("Fetching and parsing bugs from: %s, offset: %s, limit: %s ",
                         str(from_date), offset, self.max_bugs)
            raw_bugs = self.client.bugs(from_date=from_date, offset=offset,
                                        max_bugs=self.max_bugs)
            self._push_cache_queue(raw_bugs)

            data = json.loads(raw_bugs)

            if len(data['bugs']) == 0:
                break

            for bug in data['bugs']:
                bug_id = bug['id']

                bug['comments'] = self.__fetch_and_parse_comments(bug_id)
                bug['history'] = self.__fetch_and_parse_history(bug_id)
                bug['attachments'] = self.__fetch_and_parse_attachments(bug_id)
                yield bug

            self._flush_cache_queue()
            offset += self.max_bugs

    def __fetch_and_parse_comments(self, bug_id):
        logger.debug("Fetching and parsing comments from bug %s", bug_id)

        raw_comments = self.client.comments(bug_id)
        self._push_cache_queue(raw_comments)

        data = json.loads(raw_comments)
        comments = data['bugs'][str(bug_id)]['comments']

        return comments

    def __fetch_and_parse_history(self, bug_id):
        logger.debug("Fetching and parsing history from bug %s", bug_id)

        raw_history = self.client.history(bug_id)
        self._push_cache_queue(raw_history)

        data = json.loads(raw_history)
        history = data['bugs'][0]['history']

        return history

    def __fetch_and_parse_attachments(self, bug_id):
        logger.debug("Fetching and parsing attachments from bug %s", bug_id)

        raw_attachments = self.client.attachments(bug_id)
        self._push_cache_queue(raw_attachments)

        data = json.loads(raw_attachments)
        attachments = data['bugs'][str(bug_id)]

        return attachments

    @staticmethod
    def metadata_id(item):
        """Extracts the identifier from a bug item."""

        return str(item['id'])

    @staticmethod
    def metadata_updated_on(item):
        """Extracts the update time from a bug item.

        The timestamp used is extracted from 'last_change_time' field.
        This date is converted to UNIX timestamp format taking into
        account the timezone of the date.

        :param item: item generated by the backend

        :returns: a UNIX timestamp
        """
        ts = item['last_change_time']
        ts = str_to_datetime(ts)

        return ts.timestamp()


class BugzillaRESTClient:
    """Bugzilla REST API client.

    This class implements a simple client to retrieve distinct
    kind of data from a Bugzilla > 5.0 repository using its
    REST API.

    When `user` and `password` parameters are given it logs in
    the server. Further requests will use the token obtained
    during the sign in phase.

    :param base_url: URL of the Bugzilla server
    :param user: Bugzilla user
    :param password: user password
    :param api_token: api token for user; when this is provided
        `user` and `password` parameters will be ignored

    :raises BackendError: when an error occurs initilizing the
        client
    """
    URL = "%(base)s/rest/%(resource)s"

    # API resources
    RBUG = 'bug'
    RATTACHMENT = 'attachment'
    RCOMMENT = 'comment'
    RHISTORY = 'history'
    RLOGIN = 'login'

    # Resource parameters
    PBUGZILLA_LOGIN = 'login'
    PBUGZILLA_PASSWORD = 'password'
    PBUGZILLA_TOKEN = 'token'
    PLAST_CHANGE_TIME = 'last_change_time'
    PLIMIT = 'limit'
    POFFSET = 'offset'
    PORDER = 'order'
    PINCLUDE_FIELDS = 'include_fields'
    PEXCLUDE_FIELDS = 'exclude_fields'

    # Predefined values
    VCHANGE_DATE_ORDER = 'changeddate'
    VINCLUDE_ALL = '_all'
    VEXCLUDE_ATTCH_DATA = 'data'

    def __init__(self, base_url, user=None, password=None, api_token=None):
        self.base_url = base_url
        self.api_token = api_token if api_token else None

        if user is not None and password is not None:
            self.login(user, password)

    def login(self, user, password):
        """Authenticate a user in the server.

        :param user: Bugzilla user
        :param password: user password
        """
        params = {
            self.PBUGZILLA_LOGIN : user,
            self.PBUGZILLA_PASSWORD : password
        }

        try:
            r = self.call(self.RLOGIN, params)
        except requests.exceptions.HTTPError as e:
            cause = ("Bugzilla REST client could not authenticate user %s. "
                "See exception: %s") % (user, str(e))
            raise BackendError(cause=cause)

        data = json.loads(r)
        self.api_token = data['token']

    def bugs(self, from_date=DEFAULT_DATETIME, offset=None, max_bugs=MAX_BUGS):
        """Get the information of a list of bugs.

        :param from_date: retrieve bugs that where updated from that date;
            dates are converted to UTC
        :param offset: starting position for the search; i.e to return 11th
            element, set this value to 10.
        :param max_bugs: maximum number of bugs to reteurn per query
        """
        date = datetime_to_utc(from_date)
        date = date.strftime("%Y-%m-%dT%H:%M:%SZ")

        params = {
            self.PLAST_CHANGE_TIME : date,
            self.PLIMIT : max_bugs,
            self.PORDER : self.VCHANGE_DATE_ORDER,
            self.PINCLUDE_FIELDS : self.VINCLUDE_ALL
        }

        if offset:
            params[self.POFFSET] = offset

        response = self.call(self.RBUG, params)

        return response

    def comments(self, bug_id):
        """Get the comments of the given bug.

        :param bug_id: bug identifier
        """
        resource = urljoin(self.RBUG, bug_id, self.RCOMMENT)
        params = {}

        response = self.call(resource, params)

        return response

    def history(self, bug_id):
        """Get the history of the given bug.

        :param bug_id: bug identifier
        """
        resource = urljoin(self.RBUG, bug_id, self.RHISTORY)
        params = {}

        response = self.call(resource, params)

        return response

    def attachments(self, bug_id):
        """Get the attachments of the given bug.

        :param bug_id: bug identifier
        """
        resource = urljoin(self.RBUG, bug_id, self.RATTACHMENT)
        params = {
            self.PEXCLUDE_FIELDS : self.VEXCLUDE_ATTCH_DATA
        }

        response = self.call(resource, params)

        return response

    def call(self, resource, params):
        """Retrive the given resource.

        :param resource: resource to retrieve
        :param params: dict with the HTTP parameters needed to retrieve
            the given resource
        """
        url = self.URL % {'base' : self.base_url, 'resource' : resource}

        if self.api_token:
            params[self.PBUGZILLA_TOKEN] = self.api_token

        logger.debug("Bugzilla REST client requests: %s params: %s",
                     resource, str(params))

        r = requests.get(url, params=params)
        r.raise_for_status()

        return r.text


class BugzillaRESTCommand(BackendCommand):
    """Class to run BugzillaREST backend from the command line."""

    def __init__(self, *args):
        super().__init__(*args)

        self.url = self.parsed_args.url
        self.backend_user = self.parsed_args.backend_user
        self.backend_password = self.parsed_args.backend_password
        self.backend_token = self.parsed_args.backend_token
        self.max_bugs = self.parsed_args.max_bugs
        self.from_date = str_to_datetime(self.parsed_args.from_date)
        self.origin = self.parsed_args.origin
        self.outfile = self.parsed_args.outfile

        if not self.parsed_args.no_cache:
            if not self.parsed_args.cache_path:
                base_path = os.path.expanduser('~/.perceval/cache/')
            else:
                base_path = self.parsed_args.cache_path

            cache_path = os.path.join(base_path, self.url)

            cache = Cache(cache_path)

            if self.parsed_args.clean_cache:
                cache.clean()
            else:
                cache.backup()
        else:
            cache = None

        self.backend = BugzillaREST(self.url,
                                    user=self.backend_user,
                                    password=self.backend_password,
                                    api_token=self.backend_token,
                                    max_bugs=self.max_bugs,
                                    cache=cache,
                                    origin=self.origin)

    def run(self):
        """Fetch and print the bugs.

        This method runs the backend to fetch the bugs from the given
        repository. Bugs are converted to JSON objects and printed to the
        defined output.
        """
        if self.parsed_args.fetch_cache:
            bugs = self.backend.fetch_from_cache()
        else:
            bugs = self.backend.fetch(from_date=self.from_date)

        try:
            for bug in bugs:
                obj = json.dumps(bug, indent=4, sort_keys=True)
                self.outfile.write(obj)
                self.outfile.write('\n')
        except IOError as e:
            raise RuntimeError(str(e))
        except Exception as e:
            if self.backend.cache:
                self.backend.cache.recover()
            raise RuntimeError(str(e))

    @classmethod
    def create_argument_parser(cls):
        """Returns the BugzillaREST argument parser."""

        parser = super().create_argument_parser()

        # BugzillaREST options
        group = parser.add_argument_group('Bugzilla REST arguments')
        group.add_argument('--max-bugs', dest='max_bugs',
                           type=int, default=MAX_BUGS,
                           help="Maximum number of bugs requested on the same query")

        # Required arguments
        parser.add_argument('url',
                            help="URL of the Bugzilla server")

        return parser
