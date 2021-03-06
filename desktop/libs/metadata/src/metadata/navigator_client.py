#!/usr/bin/env python
# -- coding: utf-8 --
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import re

from itertools import islice

from desktop.lib.rest import resource
from desktop.lib.rest.http_client import HttpClient, RestException

from hadoop.conf import HDFS_CLUSTERS
from libsentry.privilege_checker import PrivilegeChecker
from libsentry.sentry_site import get_hive_sentry_provider

from metadata.conf import NAVIGATOR, get_navigator_auth_password, get_navigator_auth_username
from metadata.metadata_sites import get_navigator_hue_server_name


_JSON_CONTENT_TYPE = 'application/json'
LOG = logging.getLogger(__name__)
VERSION = 'v9'


def get_filesystem_host():
  host = None
  hadoop_fs = HDFS_CLUSTERS['default'].FS_DEFAULTFS.get()
  match = re.search(r"^hdfs://(?P<host>[a-z0-9\.-]+):\d[4]", hadoop_fs)
  if match:
    host = match.group('host')
  return host


class NavigatorApiException(Exception):
  pass


class NavigatorApi(object):
  """
  http://cloudera.github.io/navigator/apidocs/v3/index.html
  """

  def __init__(self, user=None):
    self._api_url = '%s/%s' % (NAVIGATOR.API_URL.get().strip('/'), VERSION)
    self._username = get_navigator_auth_username()
    self._password = get_navigator_auth_password()

    self.user = user
    self._client = HttpClient(self._api_url, logger=LOG)
    self._client.set_basic_auth(self._username, self._password)
    self._root = resource.Resource(self._client, urlencode=False) # For search_entities_interactive

    self.__headers = {}
    self.__params = ()


  def _get_types_from_sources(self, sources):
    default_entity_types = entity_types = ('DATABASE', 'TABLE', 'PARTITION', 'FIELD', 'FILE', 'VIEW', 'S3BUCKET', 'OPERATION', 'DIRECTORY')

    if 'sql' in sources or 'hive' in sources or 'impala' in sources:
      entity_types = ('TABLE', 'VIEW', 'DATABASE', 'PARTITION', 'FIELD')
      default_entity_types = ('TABLE', 'VIEW')
    elif 'hdfs' in sources:
      entity_types = ('FILE', 'DIRECTORY')
      default_entity_types  = ('FILE', 'DIRECTORY')
    elif 's3' in sources:
      entity_types = ('FILE', 'DIRECTORY', 'S3BUCKET')
      default_entity_types  = ('DIRECTORY', 'S3BUCKET')

    return default_entity_types, entity_types


  def search_entities(self, query_s, limit=100, offset=0, **filters):
    """
    Solr edismax query parser syntax.

    :param query_s: a query string of search terms (e.g. - sales quarterly);
      Currently the search will perform an OR boolean search for all terms (split on whitespace), against a whitelist
      of search_fields.
    """
    search_fields = ('originalName', 'originalDescription', 'name', 'description', 'tags')

    sources = filters.get('sources', [])
    default_entity_types, entity_types = self._get_types_from_sources(sources)

    try:
      params = self.__params

      search_terms = [term for term in query_s.strip().split()]

      query_clauses = []
      user_filters = []
      source_type_filter = []

      for term in search_terms:
        if ':' not in term:
          query_clauses.append('OR'.join(['(%s:*%s*)' % (field, term) for field in search_fields]))
        else:
          name, val = term.split(':')
          if val:
            if name == 'type':
              term = '%s:%s' % (name, val.upper().strip('*'))
              default_entity_types = entity_types # Make sure type value still makes sense for the source
            user_filters.append(term + '*') # Manual filter allowed e.g. type:VIE* ca

      filter_query = '*'

      if query_clauses:
        filter_query = 'OR'.join(['(%s)' % clause for clause in query_clauses])

      user_filter_clause = 'OR '.join(['(%s)' % f for f in user_filters]) or '*'
      source_filter_clause = 'OR'.join(['(%s:%s)' % ('type', entity_type) for entity_type in default_entity_types])
      if 's3' in sources:
        source_type_filter.append('sourceType:s3')

      filter_query = '%s AND (%s) AND (%s)' % (filter_query, user_filter_clause, source_filter_clause)
      if source_type_filter:
        filter_query += ' AND (%s)' % 'OR '.join(source_type_filter)
      if get_navigator_hue_server_name():
        filter_query += 'AND clusterName:%s' % get_navigator_hue_server_name()

      params += (
        ('query', filter_query),
        ('offset', offset),
        ('limit', NAVIGATOR.FETCH_SIZE_SEARCH.get()),
      )

      LOG.info(params)
      response = self._root.get('entities', headers=self.__headers, params=params)

      response = list(islice(self._secure_results(response), limit)) # Apply Sentry perms

      return response
    except RestException, e:
      msg = 'Failed to search for entities with search query: %s' % query_s
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def search_entities_interactive(self, query_s=None, limit=100, offset=0, facetFields=None, facetPrefix=None, facetRanges=None, filterQueries=None, firstClassEntitiesOnly=None, sources=None):
    try:
      pagination = {
        'offset': offset,
        'limit': NAVIGATOR.FETCH_SIZE_SEARCH_INTERACTIVE.get(),
      }

      entity_types = []
      fq_type = []
      if filterQueries is None:
        filterQueries = []

      if sources:
        default_entity_types, entity_types = self._get_types_from_sources(sources)

        if 'hive' in sources or 'impala' in sources:
          fq_type = default_entity_types
        elif 'hdfs' in sources:
          fq_type = entity_types
        elif 's3' in sources:
          fq_type = default_entity_types
          filterQueries.append('sourceType:s3')

        if query_s.strip().endswith('type:*'): # To list all available types
          fq_type = entity_types

      search_terms = [term for term in query_s.strip().split()] if query_s else []
      query = []
      for term in search_terms:
        if ':' not in term:
          query.append(term)
        else:
          name, val = term.split(':')
          if val: # Allow to type non default types, e.g for SQL: type:FIEL*
            if name == 'type': # Make sure type value still makes sense for the source
              term = '%s:%s' % (name, val.upper())
              fq_type = entity_types
            filterQueries.append(term)

      body = {'query': ' '.join(query) or '*'}
      if fq_type:
        filterQueries += ['{!tag=type} %s' % ' OR '.join(['type:%s' % fq for fq in fq_type])]

      if get_navigator_hue_server_name():
        filterQueries.append('clusterName:%s' % get_navigator_hue_server_name())

      body['facetFields'] = facetFields or [] # Currently mandatory in API
      if facetPrefix:
        body['facetPrefix'] = facetPrefix
      if facetRanges:
        body['facetRanges'] = facetRanges
      if filterQueries:
        body['filterQueries'] = filterQueries
      if firstClassEntitiesOnly:
        body['firstClassEntitiesOnly'] = firstClassEntitiesOnly

      data = json.dumps(body)
      LOG.info(data)
      response = self._root.post('interactive/entities?limit=%(limit)s&offset=%(offset)s' % pagination, data=data, contenttype=_JSON_CONTENT_TYPE, clear_cookies=True)

      response['results'] = list(islice(self._secure_results(response['results']), limit)) # Apply Sentry perms

      return response
    except RestException:
      msg = 'Failed to search for entities with search query %s' % json.dumps(body)
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def _secure_results(self, results):
    if NAVIGATOR.APPLY_SENTRY_PERMISSIONS.get():
      checker = PrivilegeChecker(user=self.user)
      action = 'SELECT'

      def getkey(result):
        if result['type'] == 'TABLE':
          return {u'column': None, u'table': result.get('originalName', ''), u'db': result.get('parentPath', '') and result.get('parentPath', '').strip('/'), 'server': get_hive_sentry_provider()}
        else:
          return {u'column': None, u'table': None, u'db': None, u'server': None}

      return checker.filter_objects(results, action, key=getkey)
    else:
      return results


  def suggest(self, prefix=None):
    try:
      return self._root.get('interactive/suggestions?query=%s' % (prefix or '*'))
    except RestException, e:
      msg = 'Failed to search for entities with search query: %s' % prefix
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def find_entity(self, source_type, type, name, **filters):
    """
    GET /api/v3/entities?query=((sourceType:<source_type>)AND(type:<type>)AND(originalName:<name>))
    http://cloudera.github.io/navigator/apidocs/v3/path__v3_entities.html
    """
    try:
      params = self.__params

      query_filters = {
        'sourceType': source_type,
        'type': type,
        'originalName': name,
        'deleted': 'false'
      }
      if get_navigator_hue_server_name():
        query_filters['clusterName'] = get_navigator_hue_server_name()

      for key, value in filters.items():
        query_filters[key] = value

      filter_query = 'AND'.join('(%s:%s)' % (key, value) for key, value in query_filters.items())

      params += (
        ('query', filter_query),
        ('offset', 0),
        ('limit', 2),  # We are looking for single entity, so limit to 2 to check for multiple results
      )

      response = self._root.get('entities', headers=self.__headers, params=params)

      if not response:
        raise NavigatorApiException('Could not find entity with query filters: %s' % str(query_filters))
      elif len(response) > 1:
        raise NavigatorApiException('Found more than 1 entity with query filters: %s' % str(query_filters))

      return response[0]
    except RestException, e:
      msg = 'Failed to find entity: %s' % str(e)
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def get_entity(self, entity_id):
    """
    GET /api/v3/entities/:id
    http://cloudera.github.io/navigator/apidocs/v3/path__v3_entities_-id-.html
    """
    try:
      return self._root.get('entities/%s' % entity_id, headers=self.__headers, params=self.__params)
    except RestException, e:
      msg = 'Failed to get entity %s: %s' % (entity_id, str(e))
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def update_entity(self, entity_id, **metadata):
    """
    PUT /api/v3/entities/:id
    http://cloudera.github.io/navigator/apidocs/v3/path__v3_entities_-id-.html
    """
    try:
      data = json.dumps(metadata)
      return self._root.put('entities/%s' % entity_id, params=self.__params, data=data, allow_redirects=True, clear_cookies=True)
    except RestException, e:
      msg = 'Failed to update entity %s: %s' % (entity_id, str(e))
      LOG.exception(msg)
      raise NavigatorApiException(msg)


  def get_database(self, name):
    return self.find_entity(source_type='HIVE', type='DATABASE', name=name)


  def get_table(self, database_name, table_name):
    parent_path = '\/%s' % database_name
    return self.find_entity(source_type='HIVE', type='TABLE', name=table_name, parentPath=parent_path)


  def get_field(self, database_name, table_name, field_name):
    parent_path = '\/%s\/%s' % (database_name, table_name)
    return self.find_entity(source_type='HIVE', type='FIELD', name=field_name, parentPath=parent_path)


  def get_partition(self, database_name, table_name, partition_spec):
    raise NotImplementedError


  def get_directory(self, path):
    dir_name, dir_path = self._clean_path(path)
    return self.find_entity(source_type='HDFS', type='DIRECTORY', name=dir_name, fileSystemPath=dir_path)


  def get_file(self, path):
    file_name, file_path = self._clean_path(path)
    return self.find_entity(source_type='HDFS', type='FILE', name=file_name, fileSystemPath=file_path)


  def add_tags(self, entity_id, tags):
    entity = self.get_entity(entity_id)
    new_tags = entity['tags'] or []
    new_tags.extend(tags)
    return self.update_entity(entity_id, tags=new_tags)


  def delete_tags(self, entity_id, tags):
    entity = self.get_entity(entity_id)
    new_tags = entity['tags'] or []
    for tag in tags:
      if tag in new_tags:
        new_tags.remove(tag)
    return self.update_entity(entity_id, tags=new_tags)


  def update_properties(self, entity_id, properties):
    entity = self.get_entity(entity_id)
    new_props = entity['properties'] or {}
    new_props.update(properties)
    return self.update_entity(entity_id, properties=new_props)


  def delete_properties(self, entity_id, property_keys):
    entity = self.get_entity(entity_id)
    new_props = entity['properties'] or {}
    for key in property_keys:
      if key in new_props:
        del new_props[key]
    return self.update_entity(entity_id, properties=new_props)


  def get_lineage(self, entity_id):
    """
    GET /api/v3/lineage/entityIds=:id
    http://cloudera.github.io/navigator/apidocs/v3/path__v3_lineage.html
    """
    try:
      params = self.__params

      params += (
        ('entityIds', entity_id),
      )

      return self._root.get('lineage', headers=self.__headers, params=params)
    except RestException, e:
      msg = 'Failed to get lineage for entity ID %s: %s' % (entity_id, str(e))
      LOG.exception(msg)
      raise NavigatorApiException(msg)



  def _clean_path(self, path):
    return path.rstrip('/').split('/')[-1], self._escape_slashes(path.rstrip('/'))


  def _escape_slashes(self, s):
    return s.replace('/', '\/')
