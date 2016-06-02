'''
Hubble Nova module for auditing SSL certificates

:maintainer: HubbleStack
:maturity: 20160601
:platform: Linux
:requires: SaltStack, python-OpenSSL

This audit module requires YAML data to execute. It will search the yaml data received for the topkey 'openssl'.

Sample YAML data, with intline comments:

openssl:
  google:
    data:
      tag: 'CERT-001'                   # required
      endpoint: 'www.google.com'        # required only if file is not defined
      file: null                        # required only if endpoint is not defined
      port: 443                         # optional
      notAfter: 15                      # optional
      notBefore: 2                      # optional
      fail_if_not_before: False         # optional
    description: 'google certificate'

Some words about the elements in the data dictionary:
    - tag: this is the tag of the check
    - endpoint:
        - the ssl endpoint to check
        - the module will download the SSL certificate of the endpoint
        - endpoint is required only if file is not defined (read bellow)
    file:
        - the path to the pem file containing the SSL certificate to be checked
        - the path is relative to the minion
        - the module will try to read the certificate from this file
        - if no certificate can be loaded by the OpenSSL library, the check will be failed
        - file is required only if endpoint is not defined (read more about this bellow)
    port:
        - the port is required only if both:
            - the endpoint is defined
            - https is configured on another port the 443 on the endpoint
        - WARNING: if the port is not the on configured for https on the endpoint, downloading the certificate from
          the endpoint will timeout and the check will be failed
        - if endpoint is defined but the port is not, the module will try, by default, to use port 443
    notAfter:
        - the minimum number of days left until the certificate should expire
        - if the certificate will expire in less then the value given here, the check will fail
        - if notAfter is missing, the default value is 0; basically, the if the expiration date is in the future, the
          check will be passed
    notBefore:
        - the expected number of days until the certificate becomes valid
        - this is useful only if you expect the certificate to be valid after a certain date
        - if missing, 0 is the default value (read more bellow)
    fail_if_notBefore:
        - if True, the check will fail only if notBefore is 0 (or missing): if the certificate is not valid yet, but
          it is expected to be
        - the default value is False - the check will fail only if the certificate expiration date is valid

Some notes:
    - if BOTH file and endpoint are present / missing, the check will fail; only one certificate has to be present for
      each check
    - the YAML supports also the control key, just as the other modules do

Known issues: for unknown reasons (yet), the module can fail downloading the certificate from certain endpoints. When
this happens, the check will be failed.

'''


from __future__ import absolute_import
import logging

import fnmatch
import copy
import salt.utils
import datetime
import time

import ssl

try:
    import OpenSSL
    _HAS_OPENSSL = True
except ImportError:
    _HAS_OPENSSL = False

log = logging.getLogger(__name__)

__tags__ = None
__data__ = None

def __virtual__():
    if salt.utils.is_windows():
        return False, 'This audit module only runs on linux'
    if not _HAS_OPENSSL:
        return (False, 'The python-OpenSSL library is missing')
    return True


def audit(data_list, tags, verbose=False):
    __data__ = {}
    for data in data_list:
        _merge_yaml(__data__, data)
    __tags__ = _get_tags(__data__)

    log.trace('service audit __data__:')
    log.trace(__data__)
    log.trace('service audit __tags__:')
    log.trace(__tags__)

    ret = {'Success': [], 'Failure': [], 'Controlled': []}
    for tag in __tags__:
        if fnmatch.fnmatch(tag, tags):
            for tag_data in __tags__[tag]:
                if 'control' in tag_data:
                    ret['Controlled'].append(tag_data)
                    continue

                endpoint = tag_data.get('endpoint', None)
                file = tag_data.get('file', None)
                notAfter = tag_data.get('notAfter', 0)
                notBefore = tag_data.get('notBefore', 0)
                port = tag_data.get('port', 443)
                fail_if_notBefore = tag_data.get('fail_if_not_before', False)

                if not endpoint and not file:
                    failing_reason = 'No certificate to be checked'
                    tag_data['reason'] = failing_reason
                    ret['Failure'].append(tag_data)
                    continue

                if endpoint and file:
                    failing_reason = 'Only one certificate per check is allowed'
                    tag_data['reason'] = failing_reason
                    ret['Failure'].append(tag_data)
                    continue

                x509 = _load_x509(endpoint, port) if endpoint else _load_x509(file, from_file=True)
                (passed, failing_reason) = _check_x509(x509=x509,
                                                       notBefore=notBefore,
                                                       notAfter=notAfter,
                                                       fail_if_notBefore=fail_if_notBefore)

                if passed:
                    ret['Success'].append(tag_data)
                else:
                    tag_data['reason'] = failing_reason
                    ret['Failure'].append(tag_data)

    if not verbose:
        failure = []
        success = []
        controlled = []

        tags_descriptions = set()

        for tag_data in ret['Failure']:
            tag = tag_data['tag']
            description = tag_data.get('description')
            if (tag, description) not in tags_descriptions:
                failure.append({tag: description})
                tags_descriptions.add((tag, description))

        tags_descriptions = set()

        for tag_data in ret['Success']:
            tag = tag_data['tag']
            description = tag_data.get('description')
            if (tag, description) not in tags_descriptions:
                success.append({tag: description})
                tags_descriptions.add((tag, description))

        control_reasons = set()

        for tag_data in ret['Controlled']:
            tag = tag_data['tag']
            description = tag_data.get('description')
            control_reason = tag_data.get('control', '')
            if (tag, description, control_reason) not in control_reasons:
                tag_dict = {'description': description,
                            'control': control_reason}
                controlled.append({tag: tag_dict})
                control_reasons.add((tag, description, control_reason))

        ret['Controlled'] = controlled
        ret['Success'] = success
        ret['Failure'] = failure

    if not ret['Controlled']:
        ret.pop('Controlled')

    return ret


def _merge_yaml(ret, data):
    if 'openssl' not in ret:
        ret['openssl'] = []
    for key, val in data.get('openssl', {}).iteritems():
        ret['openssl'].append({key: val})
    return ret


def _get_tags(data):
    ret = {}
    for audit_dict in data.get('openssl', {}):
        for audit_id, audit_data in audit_dict.iteritems():
            tags_dict = audit_data.get('data', {})
            tag = tags_dict.pop('tag')
            if tag not in ret:
                ret[tag] = []
            formatted_data = copy.deepcopy(tags_dict)
            formatted_data['tag'] = tag
            formatted_data['module'] = 'openssl'
            formatted_data.update(audit_data)
            formatted_data.pop('data')
            ret[tag].append(formatted_data)
    return ret


def _check_x509(x509=None, notBefore=0, notAfter=0, fail_if_notBefore=False):
    if not x509:
        return (False, 'No certificate to be checked')
    if x509.has_expired():
        return (False, 'The certificate is expired')

    stats = _get_x509_days_left(x509)

    if notAfter >= stats['notAfter']:
        return (False, 'The certificate will expire in less then {0} days'.format(notAfter))
    if notBefore <= stats['notBefore']:
        if notBefore == 0 and fail_if_notBefore:
            return (False, 'The certificate is not yet valid ({0} days left until it will be valid)'.format(stats['notBefore']))
        return (False, 'The certificate will be valid in more then {0} days'.format(notBefore))

    return (True, '')


def _load_x509(source, port=443, from_file=False):
    if not from_file:
        x509 = _load_x509_from_endpoint(source, port)
    else:
        x509 = _load_x509_from_file(source)
    return x509


def _load_x509_from_endpoint(server, port=443):
    try:
        cert = ssl.get_server_certificate((server, port))
    except Exception:
        cert = None
    if not cert:
        return None

    try:
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
    except OpenSSL.crypto.Error:
        x509 = None
    return x509


def _load_x509_from_file(cert_file_path):
    try:
        cert_file = open(cert_file_path)
    except IOError:
        return None

    try:
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert_file.read())
    except OpenSSL.crypto.Error:
        x509 = None
    return x509


def _get_x509_days_left(x509):
    date_fmt = '%Y%m%d%H%M%SZ'
    current_datetime = datetime.datetime.utcnow()
    notAfter = time.strptime(x509.get_notAfter(), date_fmt)
    notBefore = time.strptime(x509.get_notBefore(), date_fmt)

    ret = {'notAfter': (datetime.datetime(*notAfter[:6]) - current_datetime).days,
           'notBefore': (datetime.datetime(*notBefore[:6]) - current_datetime).days}

    return ret

