#!/usr/bin/env python 
#
# Copyright (c) 2014, Arista Networks, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#  - Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#  - Redistributions in binary form must reproduce the above copyright
# notice, this list of conditions and the following disclaimer in the
# documentation and/or other materials provided with the distribution.
#  - Neither the name of Arista Networks nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL ARISTA NETWORKS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# Bootstrap script
#
#    Version 0.1.0 3/1/2014
#    Written by: 
#       EOS+, Arista Networks
#
#    Revision history:
#       0.1.0 - initial release
 

import datetime
import imp
import json
import jsonrpclib
import logging
import os.path
import re
import requests
import sleekxmpp
import socket
import subprocess
import time

from logging.handlers import SysLogHandler
from subprocess import PIPE


__version__ = "0.1.0"

# This dictionary is populated once the definition is received from the server.
# Python actions may  use the attributes.
ATTRIBUTES = {}

# Server will replace this value with the correct IP address/hostname
# before responding to the bootstrap request.
SERVER = "$SERVER"

LOGGING_FACILITY = 'ztpbootstrap'
SYSLOG = '/dev/log'

CONTENT_TYPE_PYTHON = 'text/x-python'
CONTENT_TYPE_OTHER = 'text/plain'
CONTENT_TYPE_JSON = 'application/json'

TEMP = '/tmp'

COMMAND_API_SERVER = 'localhost'
COMMAND_API_USERNAME = 'ztps'
COMMAND_API_PASSWORD = 'ztps-password'
COMMAND_API_PROTOCOL = 'http'

STARTUP_CONFIG = '/mnt/flash/startup-config'

syslog_manager = None     #pylint: disable=C0103
xmpp_client = None        #pylint: disable=C0103


class ZtpBootstrapError(Exception):
    """ General exception raised by the bootstrap process """
    pass


class Node( object ):

    def __init__(self):
        Node._enable_api()

        url = '%s://%s:%s@%s/command-api' % (COMMAND_API_PROTOCOL, 
                                             COMMAND_API_USERNAME, 
                                             COMMAND_API_PASSWORD,
                                             COMMAND_API_SERVER )
        self.client = jsonrpclib.Server(url)

        try:
            self._api_enable_cmds([])
        except socket.error:
            raise ZtpBootstrapError('Unable to create Command API client')

    @classmethod
    def _cli_enable_cmd(cls, cmd):
        cmd = ['FastCli', '-p', '15', '-A', '-c', cmd]
        proc = subprocess.Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
        (out, err) = proc.communicate()
        code = proc.returncode             #pylint: disable=E1101
        return (code, out, err)

    @classmethod
    def _cli_config_cmds(cls, cmds):
        cls._cli_enable_cmd('\n'.join(['configure'] + cmds))

    @classmethod
    def _enable_api(cls):
        cls._cli_config_cmds(['username %s secret %s privilege 15' %
                              (COMMAND_API_USERNAME, 
                               COMMAND_API_PASSWORD),
                              'management api http-commands',
                              'no protocol https',
                              'protocol %s' % COMMAND_API_PROTOCOL,
                              'no shutdown'])

        _, out, _ = cls._cli_enable_cmd('show management api http-commands |'
                                        ' grep running')
        retries = 30
        while not out and retries:
            log( 'Waiting for CommandAPI to be enabled...')
            time.sleep( 1 )
            retries = retries - 1
            _, out, _ = cls._cli_enable_cmd(
                'show management api http-commands | grep running')
            

    def _api_enable_cmds(self, cmds, text_format=False):
        req_format = 'text' if text_format else 'json'
        result = self.client.runCmds(1, cmds, req_format)
        if text_format:
            return [x.values()[0] for x in result]
        else:
            return result

    def system(self):
        result = {}
        info = self._api_enable_cmds(['show version'])[0]

        result['model'] = info['modelName']
        result['version'] = info['internalVersion']
        result['systemmac'] = info['systemMacAddress']
        result['serialnumber'] = info['serialNumber']
        
        return result

    def neighbors(self):
        result = {}
        info = self._api_enable_cmds(['show lldp neighbors'])[0]
        result['neighbors'] = {}
        for entry in info['lldpNeighbors']:
            neighbor = {}
            neighbor['device'] = entry['neighborDevice']
            neighbor['remote_interface'] = entry['neighborPort']
            if result['neighbors'][entry['port']]:
                result['neighbors'][entry['port']] += [neighbor]
            else:
                result['neighbors'][entry['port']] = [neighbor]
        return result
    
    def details(self):
        return dict(self.system().items() + 
                    self.neighbors().items())

    def has_startup_config(self):                    #pylint: disable=R0201
        return os.path.isfile(STARTUP_CONFIG)


class SyslogManager(object):
    
    def __init__(self):
        self.log = logging.getLogger('ztpbootstrap')
        self.log.setLevel(logging.DEBUG)
        self.formatter = logging.Formatter('ztp-bootstrap: %(levelname)s: '
                                           '%(message)s')

        # syslog to localhost enabled by default
        self._add_syslog_handler()

    def _add_handler(self, handler, level=None):
        if level is None:
            level = logging.DEBUG
        else:
            level = logging.getLevelName(level)           
            
        handler.setLevel(level)
        handler.setFormatter(self.formatter)
        self.log.addHandler(handler)

    def _add_syslog_handler(self):
        log('SyslogManager: adding localhost handler')
        self._add_handler(SysLogHandler(address=SYSLOG))

    def _add_file_handler(self, filename, level=None):
        log('SyslogManager: adding file handler (%s=%s)' % 
            (filename, level))
        self._add_handler(logging.FileHandler(filename), level)

    def _add_remote_syslog_handler(self, host, port, level=None):
        log('SyslogManager: adding remote handler (%s:%s=%s)' % 
            (host, port, level))
        self._add_handler(SysLogHandler(address=(host, port)), level)

    def add_handlers(self, handler_config):
        for entry in handler_config:
            match = re.match('^file:(.+)',
                             entry['destination'])
            if match:
                self._add_file_handler(match.groups()[ 0 ], 
                                                entry['level'])
            else:
                match = re.match('^(.+):(.+)',
                                 entry['destination'])
                if match:
                    self._add_remote_syslog_handler(match.groups()[ 0 ], 
                                                   match.groups()[ 1 ],
                                                   entry['level'])
                else:
                    log('SyslogManager: Unable to create syslog handler for'
                        ' %s' % str( entry ), error=True)


class ServerConnection(object):

    def __init__(self, url):
        self.url = url

    def _http_request(self, path, method='get', headers=None,
                      payload=None, files=None):
        if headers is None:
            headers = {}
        if files is None:
            files = []

        request_files = []
        for entry in files:
            request_files[entry] = open(entry,'rb')
            
        full_url = '%s/%s' % (self.url, path)
        if method == 'get':
            log('ServerConnection: GET %s' % full_url)
            response = requests.get(full_url,
                                    data=json.dumps(payload),
                                    header=headers,
                                    files=request_files)
        elif method == 'post':
            log('ServerConnection: POST %s' % full_url)
            response = requests.post(full_url,
                                     data=json.dumps(payload),
                                     header=headers,
                                     files=request_files)
        elif method == 'put':
            log('ServerConnection: PUT %s' % full_url)
            response = requests.put(full_url,
                                    data=json.dumps(payload),
                                    header=headers,
                                    files=request_files)
        else:
            log('ServerConnection: Unknown method %s' % method, 
                error=True)

        return response

    def get_config(self):
        log('ServerConnection: Retrieving server config')
        return self._http_request( 'bootstrap/config' ).json()

    def post_definition(self, node):
        log('ServerConnection: Retrieving server config')
        headers = {'content-type': CONTENT_TYPE_JSON}
        return self._http_request('bootstrap/config',
                                  method='post',
                                  headers=headers,
                                  payload=node).json()

    def get_action(self, action):
        log('ServerConnection: Retrieving server config')
        return self._http_request('actions/%s' % action)


class XmppClient(object):
    #pylint: disable=W0613

    def __init__(self, user, domain, password,
                 server, port, nick, rooms):

        self.user = user
        self.domain = domain
        self.password = password
        self.server = server
        self.port = port
        self.nick = nick
        self.rooms = rooms

        if self.rooms is None:
            self.rooms = []

        self.jid = '%s@%s' % (user, domain)
        self.connected = False

        self.client = self._client()
        self.connect()

    def _client(self):
        log('XmppClient: Configuring client for %s' % self.jid)
        client = sleekxmpp.ClientXMPP(self.jid, self.password)

        client.add_event_handler('session_start', self._session_connected)
        client.add_event_handler('connect', self._session_connected)
        client.add_event_handler('disconnected', self._session_disconnected)

        # # Multi-User Chat
        client.register_plugin('xep_0045')
        # XMPP Ping
        client.register_plugin('xep_0199') 
        # Service Discovery
        client.register_plugin('xep_0030') 

        return client

    def _session_connected(self, event):
        log('XmppClient: Session connected (%s)' % self.jid)
        self.client.get_roster()
        self.client.send_presence()

        # Joining rooms
        for room in self.rooms:
            self.client.plugin['xep_0045'].joinMUC(room, self.nick, 
                                                   wait=True)
            log('XmppClient: Joining room %s as %s' % 
                (room, self.nick))

        self.connected = True

    def _session_disconnected(self, event):
        log('XmppClient: Session disconnected (%s)' % self.jid)
        self.connected = False

    def connect(self):
        log('XmppClient: Connecting to XMPP server %s:%s as %s' % 
            (self.server, self.port, self.jid))

        retries = 3
        while not retries:
            if self.client.connect((self.server, self.port)):
                self.client.process(block=False)
                break
            else:
                log('XmppClient: Failed to connect to XMPP server %s:%s '
                    'as %s. Retrying in 10 seconds...' % 
                    (self.server, self.port, self.jid))                    
                time.sleep( 10 )
                retries = retries - 1

    def message(self, message):
        if not self.connected:
            log('XmppClient: Failed to send mesage because %s ' 
                'is not connected to server'% self.jid, error=True)
            return

        log('XmppClient: %s says %s' % (self.jid, self.nick))
        for room in self.rooms:
            self.client.send_message(mto=room,
                                     mbody=message,
                                     mtype='groupchat',
                                     mfrom=self.nick)

    def disconnect(self):
        if not self.connected:
            return

        for room in self.rooms:
            log('XmppClient: Leaving room %s (%s)' % 
                (room, self.nick))           
            self.client.plugin['xep_0045'].leaveMUC(room, self.nick)

        self.client.disconnect(wait=True)
        self.connected = False


def apply_config(config):
    log('Applying server config')
    global xmpp_client                      #pylint: disable=W0603

    log('Configuring syslog')
    syslog_manager.add_handlers(config['logging'])


    log('Configuring XMPP')
    xmpp_config = config['xmpp']
    xmpp_client = XmppClient(xmpp_config['username'], 
                             xmpp_config['domain'], 
                             xmpp_config['password'],
                             xmpp_config['server'], 
                             xmpp_config.get('port', 5222),
                             xmpp_config.get('nickname', 
                                             xmpp_config['username']),
                             xmpp_config.get('rooms', []))


def log(msg, error=False):
    if syslog_manager:
        if error:
            syslog_manager.log.error(msg)
        else:
            syslog_manager.log.info(msg)           

    timestamp = datetime.datetime.now().strftime( '%Y-%m-%d_%H:%M:%S' )
    msg = '%s: ztp-bootstrap: %s%s' % (timestamp, 
                                       'ERROR: ' if error else '',
                                       msg)

    if xmpp_client.connected:
        xmpp_client.message(msg)

    print msg


def download_action(server, action):
    response = server.get_action(action)

    filename = os.path.join(TEMP, action)
    with open(filename, 'wb') as action_file:
        for chunk in response.iter_content(chunk_size=1024):
            if chunk:
                action_file.write(chunk)
        action_file.close()

    os.chmod(filename, 0777)

    return (response.headers['content-type'], filename)
    

def execute_action(server, action, action_details):
    log('Downloading action %s' % action)
    content_type, filename = download_action(server, action)

    log('Executing action %s' % action)
    if 'onstart' in action_details:
        log('ACTION %s:%s' % (action, action_details['onstart']))

    if content_type == CONTENT_TYPE_PYTHON:
        try:
            imp.load_source(action, filename)
            log('Action %s executed succesfully' % action)
            if 'onsuccess' in action_details:
                log('ACTION %s:%s' % (action, action_details['onsuccess']))
        except Exception as err:                  #pylint: disable=W0703
            log('Executing %s failed: %s' % (action, err), error=True)
            if 'onfailure' in action_details:
                log('ACTION %s:%s' % (action, action_details['onfailure']))
    elif content_type == CONTENT_TYPE_OTHER:
        return_code = subprocess.call(filename, shell=True)
        if return_code:
            log('Executing %s failed: returncode=%s' % (action, return_code), 
                error=True)
            if 'onfailure' in action_details:
                log('ACTION %s:%s' % (action, action_details['onfailure']))
        else:
            log('Action %s executed succesfully' % action)
            if 'onsuccess' in action_details:
                log('ACTION %s:%s' % (action, action_details['onsuccess']))
    else:
        log('Unable to execute action %s - unknown contenty-type %s' % 
            (action, content_type), error=True)

def main():
    #pylint: disable=W0603
    global ATTRIBUTES, syslog_manager                   
    syslog_manager = SyslogManager()
    server = ServerConnection(SERVER)

    # Retrieve and apply logging/XMPP configuration from server
    config = server.get_config()
    apply_config(config)

    # Get definition
    node = Node()
    definition = server.post_definition(node.details())

    # Execute actions
    definition_name = definition['name']
    log('Applying definition %s' % definition_name)

    ATTRIBUTES = definition['attributes']
    for action, details in definition['actions'].iteritems():
        missing_attr = [x for x in details['requires'] if x not in ATTRIBUTES]
        if missing_attr:
            log('Failed to load action %s because the following '
                'attributes are missing: %s' % (action, 
                                                ', '.join(missing_attr)),
                error=True)            
            continue
        execute_action(server, action, details)

    log('Definition %s applied successfully' % definition_name)

    # Check for startup-config
    if not node.has_startup_config():
        log('Startup configuration is missing at the end of the '
            'bootstrap process', error=True)        

if __name__ == '__main__':
    main()
