# -*- coding: utf-8 -*-
import logging
import os
import socket
from importlib import import_module
from biothings.utils.www.log import get_hipchat_logger
import json

# Error class
class BiothingConfigError(Exception):
    pass

class BiothingWebSettings(object):
    def __init__(self, config='biothings.www.settings.default'): 
        self.config_mod = import_module(config)
        try:
            with open(os.path.abspath(self.config_mod.JSONLD_CONTEXT_PATH), 'r') as json_file:
                self._jsonld_context = json.load(json_file)
        except:
            self._jsonld_context = {}

        # for metadata dev
        self._app_git_repo = os.path.abspath(self.APP_GIT_REPOSITORY) if hasattr(self, 'APP_GIT_REPOSITORY') else os.path.abspath('.')
        if not (self._app_git_repo and os.path.exists(self._app_git_repo) and 
            os.path.isdir(self._app_git_repo) and os.path.exists(os.path.join(self._app_git_repo, '.git'))):
            self._app_git_repo = None
        
        # for logging exceptions to hipchat
        if self.HIPCHAT_ROOM and self.HIPCHAT_AUTH_TOKEN:
            try:
                _socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                _socket.connect(self.HIPCHAT_AUTO_FROM_SOCKET_CONNECTION)  # set up socket
                _from = _socket.getsockname()[0] # try to get local ip as the "from" key
            except:
                _from = None
            self._hipchat_logger = get_hipchat_logger(hipchat_room=self.HIPCHAT_ROOM, 
                hipchat_auth_token=self.HIPCHAT_AUTH_TOKEN, hipchat_msg_from=_from, 
                hipchat_log_format=getattr(self, 'HIPCHAT_MESSAGE_FORMAT', None), 
                hipchat_msg_color=self.HIPCHAT_MESSAGE_COLOR)
        else:
            self._hipchat_logger = None

        # validate these settings?
        self.validate()
    
    def __getattr__(self, name):
        try:
            return getattr(self.config_mod, name)
        except AttributeError:
            raise AttributeError("No setting named '{}' was found, check configuration module.".format(name))

    def set_debug_level(self, debug=False):
        ''' Are we debugging? '''
        self._DEBUG = debug
        return self
    
    def generate_app_list(self):
        ''' Generates the APP_LIST for tornado for this project, basically just adds the settings 
            to kwargs in every handler's initialization. '''
        return [(endpoint_regex, handler, {"web_settings": self}) for (endpoint_regex, handler) in self.APP_LIST]

    def validate(self):
        ''' validates this object '''
        pass

class BiothingESWebSettings(BiothingWebSettings):
    ''' subclass with functions specific to elasticsearch backend '''
    def __init__(self, config='biothings.www.settings.default'):
        super(BiothingESWebSettings, self).__init__(config)

        # get es client for web
        self.es_client = self.get_es_client()

    def get_es_client(self):
        ''' get the es client for this app '''
        from elasticsearch import Elasticsearch
        return Elasticsearch(self.ES_HOST, timeout=getattr(self, 'ES_CLIENT_TIMEOUT', 120))