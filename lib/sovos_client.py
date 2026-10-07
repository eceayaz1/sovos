"""Sovos (eski FIT / Foriba) e-Fatura SOAP istemcisi.

ahmeti/sovos-api PHP kütüphanesindeki ``InvoiceService`` sınıfının Python karşılığıdır.
Odoo'dan bağımsızdır; Odoo tarafı ``_get_sovos_client(company)`` ile kullanır.
"""
import base64
import io
import logging
import time
import zipfile

import requests
from lxml import etree

_logger = logging.getLogger(__name__)

URL_TEST = 'https://efaturawstest.fitbulut.com/ClientEInvoiceServices/ClientEInvoiceServicesPort.svc'
URL_PROD = 'https://efaturaws.fitbulut.com/ClientEInvoiceServices/ClientEInvoiceServicesPort.svc'

NS_SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
# Sovos servisinin namespace'i gerçekten tek eğik çizgiyle tanımlı ("http:/").
NS_EIN = 'http:/fitcons.com/eInvoice/'


class SovosError(Exception):
    def __init__(self, message, code=None, response_text=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.response_text = response_text

    def __str__(self):
        return f"[{self.code}] {self.message}" if self.code else self.message


class SovosUnauthorizedError(SovosError):
    pass


class SovosSchemaValidationError(SovosError):
    pass


def _get_sovos_client(company, timeout=None):
    return SovosEInvoiceClient(
        username=company.sudo().l10n_tr_sovos_username,
        password=company.sudo().l10n_tr_sovos_password,
        test=company.l10n_tr_sovos_use_test_env,
        timeout=timeout,
    )


def _localname(node):
    return etree.QName(node).localname


def _node_to_dict(node):
    """ Basit (tek seviyeli) bir yanıt düğümünü {localname: text} sözlüğüne çevirir. """
    return {_localname(child): (child.text or '').strip() for child in node if isinstance(child.tag, str)}


def _iter_children(parent, localname):
    return [child for child in parent if isinstance(child.tag, str) and _localname(child) == localname]


def _decode_doc_data(doc_data):
    """ base64 DocData'yı çözer; içerik zip ise içindeki ilk dosyayı döndürür. """
    raw = base64.b64decode(doc_data or b'')
    if raw[:2] == b'PK':
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = [name for name in zf.namelist() if not name.endswith('/')]
            if names:
                return zf.read(names[0])
    return raw


def _zip_document(filename, content):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    return buffer.getvalue()


class SovosEInvoiceClient:
    def __init__(self, username, password, test=False, timeout=None):
        if not username or not password:
            raise SovosError("Sovos web servis kullanıcı adı / şifresi tanımlı değil.")
        self.url = URL_TEST if test else URL_PROD
        self.timeout = min(timeout or 30, 120)
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.headers.update({
            'Content-Type': 'text/xml;charset=UTF-8',
            'Accept': 'text/xml',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        })

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._session.close()

    # -------------------------------------------------------------------------
    # LOW LEVEL
    # -------------------------------------------------------------------------

    def _build_envelope(self, method_name, params):
        envelope = etree.Element(etree.QName(NS_SOAP, 'Envelope'), nsmap={'soapenv': NS_SOAP, 'ein': NS_EIN})
        etree.SubElement(envelope, etree.QName(NS_SOAP, 'Header'))
        body = etree.SubElement(envelope, etree.QName(NS_SOAP, 'Body'))
        request_node = etree.SubElement(body, etree.QName(NS_EIN, method_name))
        for key, value in params:
            values = value if isinstance(value, (list, tuple)) else [value]
            for val in values:
                if val is None or val is False or val == '':
                    continue
                if val is True:
                    val = 'true'
                etree.SubElement(request_node, etree.QName(NS_EIN, key)).text = str(val)
        return etree.tostring(envelope, encoding='UTF-8', xml_declaration=False)

    def _call(self, soap_action, method_name, params):
        """ SOAP isteğini gönderir, hata (Fault) varsa SovosError fırlatır, yoksa Body'nin ilk çocuğunu döndürür.

        :param params: (anahtar, değer) listesi; sıra WSDL'deki sırayla aynı olmalıdır.
        """
        payload = self._build_envelope(method_name, params)
        start = time.monotonic()
        try:
            response = self._session.post(
                self.url,
                data=payload,
                headers={'SOAPAction': soap_action},
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as e:
            _logger.warning("Sovos %s ağ hatası: %s", soap_action, e)
            raise SovosError(f"Sovos servisine bağlanılamadı: {e}") from e
        _logger.info("Sovos %s -> HTTP %s (%.2fs)", soap_action, response.status_code, time.monotonic() - start)

        if response.status_code == 401:
            raise SovosUnauthorizedError("Sovos kullanıcı adı veya şifresi hatalı (Unauthorized).", code=401)

        try:
            root = etree.fromstring(response.content, parser=etree.XMLParser(huge_tree=True, resolve_entities=False))
        except etree.XMLSyntaxError as e:
            raise SovosError(
                f"Sovos yanıtı okunamadı (HTTP {response.status_code}).",
                code=response.status_code,
                response_text=response.text[:2000],
            ) from e

        body = root.find(f'{{{NS_SOAP}}}Body')
        if body is None:
            raise SovosError("Sovos yanıtında SOAP Body bulunamadı.", response_text=response.text[:2000])

        fault = body.find(f'{{{NS_SOAP}}}Fault')
        if fault is not None:
            self._raise_fault(fault, response.text)

        result = next((child for child in body if isinstance(child.tag, str)), None)
        if result is None:
            raise SovosError("Sovos yanıtı boş döndü.", response_text=response.text[:2000])
        return result

    def _raise_fault(self, fault, response_text):
        faultcode = (fault.findtext('faultcode') or '').strip()
        faultstring = (fault.findtext('faultstring') or '').strip()
        code, message = None, faultstring
        detail = fault.find('detail')
        if detail is not None:
            processing_fault = next((child for child in detail if isinstance(child.tag, str)), None)
            if processing_fault is not None:
                values = _node_to_dict(processing_fault)
                code = values.get('Code') or None
                message = values.get('Message') or values.get('Text') or faultstring

        if faultstring == 'Unauthorized':
            raise SovosUnauthorizedError("Sovos kullanıcı adı veya şifresi hatalı (Unauthorized).", code=code)
        if faultstring == 'Şema validasyon hatası':
            raise SovosSchemaValidationError(message or "Bilinmeyen bir şema hatası oluştu.", code=code, response_text=response_text)
        raise SovosError(message or f"Sovos hatası ({faultcode})", code=code or faultcode, response_text=response_text)

    # -------------------------------------------------------------------------
    # E-FATURA SERVİSİ
    # -------------------------------------------------------------------------

    def get_user_list(self, identifier, vkn_tckn, role='PK', filter_vkn_tckn=None, registered_after=None):
        """ e-Fatura kayıtlı kullanıcı sorgulama (getUserList).

        :return: [{'Identifier', 'Alias', 'Title', 'Type', 'RegisterTime', 'FirstCreationTime'}, ...]
        """
        result = self._call('getUserList', 'getUserListRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('Role', role),
            ('RegisteredAfter', registered_after),
            ('Filter_VKN_TCKN', filter_vkn_tckn),
        ])
        return [_node_to_dict(user) for user in _iter_children(result, 'User')]

    def get_raw_user_list(self, identifier, vkn_tckn, role='PK'):
        """ Tüm e-Fatura/e-İrsaliye kayıtlı kullanıcılar listesi (getRAWUserList).

        :return: zip dosyasının ham içeriği (bytes)
        """
        result = self._call('getRAWUserList', 'getRAWUserListRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('Role', role),
        ])
        doc_data = next((child.text for child in _iter_children(result, 'DocData')), None)
        return base64.b64decode(doc_data or b'')

    def get_ubl_list(self, identifier, vkn_tckn, doc_type='INVOICE', type='INBOUND', from_date=None, to_date=None, uuid=None):
        """ Gelen/giden belge listesi (getUBLList).

        :param doc_type: INVOICE, ENVELOPE veya APP_RESP
        :param type: INBOUND veya OUTBOUND
        :param from_date/to_date: ISO 8601 tarih-saat metni, örn. 2026-01-01T00:00:00+03:00
        :return: [{'UUID', 'Identifier', 'VKN_TCKN', 'EnvType', 'InsertDateTime', 'ID', 'EnvUUID'}, ...]
        """
        result = self._call('getUBLList', 'getUBLListRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('UUID', uuid),
            ('DocType', doc_type),
            ('Type', type),
            ('FromDate', from_date),
            ('ToDate', to_date),
            ('FromDateSpecified', bool(from_date)),
            ('ToDateSpecified', bool(to_date)),
        ])
        return [_node_to_dict(ubl) for ubl in _iter_children(result, 'UBLList')]

    def get_ubl(self, identifier, vkn_tckn, uuid, doc_type='INVOICE', type='INBOUND', parameters='DOC_DATA'):
        """ Belgenin UBL içeriğini indirir (getUBL).

        :param uuid: tek UUID ya da UUID listesi
        :return: her belge için çözülmüş XML içeriği (bytes) listesi
        """
        result = self._call('getUBL', 'getUBLRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('UUID', uuid),
            ('DocType', doc_type),
            ('Type', type),
            ('Parameters', parameters),
        ])
        return [_decode_doc_data(node.text) for node in _iter_children(result, 'DocData')]

    def get_invoice_view(self, uuid, identifier, vkn_tckn, type='INVOICE', doc_type='PDF', cust_inv_id=None):
        """ Faturanın PDF/HTML görüntüsünü indirir (getInvoiceView).

        :param doc_type: PDF, PDF_DEFAULT veya HTML
        :return: çözülmüş dosya içeriği (bytes)
        """
        result = self._call('getInvoiceView', 'getInvoiceViewRequest', [
            ('UUID', uuid),
            ('CustInvID', cust_inv_id),
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('Type', type),
            ('DocType', doc_type),
        ])
        doc_data = next((child.text for child in _iter_children(result, 'DocData')), None)
        return _decode_doc_data(doc_data)

    def get_envelope_status(self, identifier, vkn_tckn, uuid, parameters=None):
        """ Zarf durumu sorgulama (getEnvelopeStatus).

        :param uuid: zarf UUID'si ya da listesi
        :return: [{'UUID', 'IssueDate', 'DocumentTypeCode', 'DocumentType', 'ResponseCode', 'Description', 'DocData'}, ...]
        """
        result = self._call('getEnvelopeStatus', 'getEnvelopeStatusRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('UUID', uuid),
            ('Parameters', parameters),
        ])
        return [_node_to_dict(resp) for resp in _iter_children(result, 'Response')]

    def get_inv_responses(self, identifier, vkn_tckn, uuid, type='OUTBOUND', parameters=None):
        """ Ticari faturalara verilen KABUL/RED yanıtlarını sorgular (getInvResponses).

        :param uuid: fatura UUID'si (ETTN) ya da listesi
        :return: [{'InvoiceUUID': str, 'InvResponses': [{'EnvUUID', 'UUID', 'ID', 'InsertDateTime', 'IssueDate', 'ARType', 'ARNotes'}]}]
        """
        result = self._call('getInvResponses', 'getInvResponsesRequest', [
            ('Identifier', identifier),
            ('VKN_TCKN', vkn_tckn),
            ('UUID', uuid),
            ('Type', type),
            ('Parameters', parameters),
        ])
        responses = []
        for resp in _iter_children(result, 'Response'):
            responses.append({
                'InvoiceUUID': next(((c.text or '').strip() for c in _iter_children(resp, 'InvoiceUUID')), ''),
                'InvResponses': [_node_to_dict(r) for r in _iter_children(resp, 'InvResponses')],
            })
        return responses

    def send_ubl(self, vkn_tckn, sender_identifier, receiver_identifier, document_uuid, xml_content, doc_type='INVOICE'):
        """ UBL belgesini zipleyip gönderir (sendUBL).

        Zip içindeki dosya adı belge UUID'si ile aynı olmalıdır.

        :param doc_type: INVOICE veya APP_RESP
        :return: [{'EnvUUID', 'UUID', 'ID', 'CustInvID'}, ...]
        """
        if isinstance(xml_content, str):
            xml_content = xml_content.encode()
        zip_content = _zip_document(f'{document_uuid}.xml', xml_content)
        result = self._call('sendUBL', 'sendUBLRequest', [
            ('VKN_TCKN', vkn_tckn),
            ('SenderIdentifier', sender_identifier),
            ('ReceiverIdentifier', receiver_identifier),
            ('DocType', doc_type),
            ('DocData', base64.b64encode(zip_content).decode()),
        ])
        return [_node_to_dict(resp) for resp in _iter_children(result, 'Response')]
