import logging
import uuid
from datetime import timedelta

from lxml import etree
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from odoo.addons.l10n_tr_sovos_einvoice.lib.sovos_client import SovosError, _get_sovos_client

_logger = logging.getLogger(__name__)

# GİB zarf durum kodları
ENVELOPE_SUCCESS_CODES = {'1300'}
ENVELOPE_PENDING_CODES = {'1000', '1100', '1200', '1220'}

NS_UBL_APP_RESP = 'urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2'
NS_CAC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2'
NS_CBC = 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2'


class AccountMove(models.Model):
    _inherit = 'account.move'

    l10n_tr_gib_invoice_scenario = fields.Selection(
        selection_add=[('TICARIFATURA', "Ticari")],
        ondelete={'TICARIFATURA': 'set default'},
    )
    l10n_tr_sovos_state = fields.Selection(
        selection=[
            ('not_sent', "Gönderilmedi"),
            ('sent', "Gönderildi (yanıt bekleniyor)"),
            ('succeed', "Başarılı"),
            ('error', "Hata"),
            ('received', "Alındı"),
        ],
        string="Sovos Durumu",
        default='not_sent',
        copy=False,
        readonly=True,
        tracking=True,
    )
    l10n_tr_sovos_envelope_uuid = fields.Char(string="Sovos Zarf UUID", copy=False, readonly=True)
    l10n_tr_sovos_document_id = fields.Char(string="GİB Fatura No", copy=False, readonly=True)
    l10n_tr_sovos_status_code = fields.Char(string="Sovos Durum Kodu", copy=False, readonly=True)
    l10n_tr_sovos_status_description = fields.Char(string="Sovos Durum Açıklaması", copy=False, readonly=True)
    l10n_tr_sovos_profile_id = fields.Char(string="Fatura Senaryosu (Profil)", copy=False, readonly=True)
    l10n_tr_sovos_sender_alias = fields.Char(string="Gönderici Etiketi (GB)", copy=False, readonly=True)
    l10n_tr_sovos_response = fields.Selection(
        selection=[('KABUL', "Kabul"), ('RED', "Red")],
        string="Ticari Fatura Yanıtı",
        copy=False,
        readonly=True,
        tracking=True,
    )
    l10n_tr_sovos_response_note = fields.Char(string="Yanıt Açıklaması", copy=False, readonly=True)

    # -------------------------------------------------------------------------
    # CRUD / FLOW
    # -------------------------------------------------------------------------

    def button_draft(self):
        # EXTENDS account
        for move in self:
            if move.l10n_tr_sovos_state in ('sent', 'succeed'):
                raise UserError(_("Sovos'a gönderilmiş bir faturayı taslağa çekemezsiniz: %s", move.display_name))
            if move.l10n_tr_sovos_state == 'error':
                move.message_post(body=_(
                    "GİB tarafından reddedilen fatura yeniden kullanılamaz. Lütfen yeni bir fatura oluşturun."
                ))
        return super().button_draft()

    def _post(self, soft=True):
        for move in self:
            if move.l10n_tr_sovos_state == 'error':
                raise UserError(_(
                    "GİB tarafından reddedilen fatura yeniden kullanılamaz. Lütfen yeni bir fatura oluşturun."
                ))
        return super()._post(soft=soft)

    def _l10n_tr_sovos_check_company(self):
        company = self.company_id[:1] or self.env.company
        if not company._l10n_tr_sovos_is_active():
            raise UserError(_("%s şirketi için Sovos kullanıcı bilgileri tanımlı değil.", company.name))
        if not company._l10n_tr_sovos_vkn():
            raise UserError(_("%s şirketinin Vergi Numarası tanımlı değil.", company.name))
        return company

    def _l10n_tr_sovos_own_identifier(self):
        """ Sorgularda kullanılacak kendi etiketimiz: giden belgelerde GB, gelenlerde PK. """
        self.ensure_one()
        if self.is_purchase_document(include_receipts=True):
            return self.company_id.l10n_tr_sovos_pk_alias
        return self.company_id.l10n_tr_sovos_gb_alias

    # -------------------------------------------------------------------------
    # GÖNDERİM
    # -------------------------------------------------------------------------

    def _l10n_tr_sovos_send_einvoice(self, xml_content, receiver_alias):
        """ UBL-TR faturayı Sovos'a gönderir. Hata durumunda SovosError fırlatır. """
        self.ensure_one()
        company = self._l10n_tr_sovos_check_company()
        if not company.l10n_tr_sovos_gb_alias:
            raise SovosError(_("Şirketin Gönderici Birim (GB) etiketi tanımlı değil."))
        if not self.l10n_tr_nilvera_uuid:
            self.l10n_tr_nilvera_uuid = str(uuid.uuid4())

        with _get_sovos_client(company) as client:
            responses = client.send_ubl(
                vkn_tckn=company._l10n_tr_sovos_vkn(),
                sender_identifier=company.l10n_tr_sovos_gb_alias,
                receiver_identifier=receiver_alias,
                document_uuid=self.l10n_tr_nilvera_uuid,
                xml_content=xml_content,
                doc_type='INVOICE',
            )

        response = next(
            (r for r in responses if r.get('UUID') == self.l10n_tr_nilvera_uuid),
            responses[0] if responses else {},
        )
        self.write({
            'l10n_tr_sovos_state': 'sent',
            'l10n_tr_sovos_envelope_uuid': response.get('EnvUUID'),
            'l10n_tr_sovos_document_id': response.get('ID'),
            'l10n_tr_sovos_status_code': False,
            'l10n_tr_sovos_status_description': False,
            'is_move_sent': True,
        })
        self.message_post(body=Markup("%s<br/>%s: %s<br/>%s: %s") % (
            _("Fatura Sovos'a gönderildi."),
            _("Zarf UUID"), response.get('EnvUUID') or '-',
            _("ETTN"), self.l10n_tr_nilvera_uuid,
        ))

    # -------------------------------------------------------------------------
    # DURUM SORGULAMA
    # -------------------------------------------------------------------------

    def _l10n_tr_sovos_update_status(self):
        for company, moves in self.filtered('l10n_tr_sovos_envelope_uuid').grouped('company_id').items():
            if not company._l10n_tr_sovos_is_active():
                continue
            vkn = company._l10n_tr_sovos_vkn()
            with _get_sovos_client(company) as client:
                for move in moves:
                    try:
                        responses = client.get_envelope_status(
                            identifier=company.l10n_tr_sovos_gb_alias,
                            vkn_tckn=vkn,
                            uuid=move.l10n_tr_sovos_envelope_uuid,
                        )
                    except SovosError as e:
                        move.message_post(body=_("Sovos zarf durumu alınamadı: %s", e))
                        continue
                    if responses:
                        move._l10n_tr_sovos_apply_envelope_status(responses[0])

                    if (
                        move.l10n_tr_sovos_state == 'succeed'
                        and move.l10n_tr_gib_invoice_scenario == 'TICARIFATURA'
                        and not move.l10n_tr_sovos_response
                    ):
                        move._l10n_tr_sovos_update_commercial_response(client, company)

    def _l10n_tr_sovos_apply_envelope_status(self, response):
        self.ensure_one()
        code = (response.get('ResponseCode') or '').strip()
        description = response.get('Description')
        if code in ENVELOPE_SUCCESS_CODES:
            state = 'succeed'
        elif code in ENVELOPE_PENDING_CODES or not code:
            state = 'sent'
        else:
            state = 'error'

        if (code, state) == (self.l10n_tr_sovos_status_code, self.l10n_tr_sovos_state):
            return
        self.write({
            'l10n_tr_sovos_state': state,
            'l10n_tr_sovos_status_code': code,
            'l10n_tr_sovos_status_description': description,
        })
        if state == 'error':
            self.message_post(body=Markup("%s<br/>%s - %s") % (
                _("Fatura GİB tarafından alıcıya iletilemedi."), code, description or '',
            ))

    def _l10n_tr_sovos_update_commercial_response(self, client, company):
        """ Ticari faturaya alıcının verdiği KABUL/RED yanıtını sorgular. """
        self.ensure_one()
        try:
            responses = client.get_inv_responses(
                identifier=company.l10n_tr_sovos_gb_alias,
                vkn_tckn=company._l10n_tr_sovos_vkn(),
                uuid=self.l10n_tr_nilvera_uuid,
                type='OUTBOUND',
            )
        except SovosError as e:
            _logger.warning("Sovos ticari fatura yanıtı alınamadı (%s): %s", self.name, e)
            return
        for response in responses:
            for inv_response in response['InvResponses']:
                ar_type = (inv_response.get('ARType') or '').upper()
                if ar_type in ('KABUL', 'RED'):
                    self.write({
                        'l10n_tr_sovos_response': ar_type,
                        'l10n_tr_sovos_response_note': inv_response.get('ARNotes'),
                    })
                    self.message_post(body=_("Alıcı ticari faturaya yanıt verdi: %(type)s %(note)s",
                                             type=ar_type, note=inv_response.get('ARNotes') or ''))
                    return

    def action_l10n_tr_sovos_update_status(self):
        self._l10n_tr_sovos_check_company()
        self._l10n_tr_sovos_update_status()

    # -------------------------------------------------------------------------
    # PDF
    # -------------------------------------------------------------------------

    def _l10n_tr_sovos_add_pdf(self, client):
        self.ensure_one()
        pdf_content = client.get_invoice_view(
            uuid=self.l10n_tr_nilvera_uuid,
            identifier=self._l10n_tr_sovos_own_identifier(),
            vkn_tckn=self.company_id._l10n_tr_sovos_vkn(),
            type='INVOICE',
            doc_type='PDF',
        )
        if not pdf_content:
            return
        reference = self.ref if self.is_purchase_document(include_receipts=True) else self.name
        attachment = self.env['ir.attachment'].create({
            'name': f"{(reference or self.l10n_tr_nilvera_uuid).replace('/', '_')}_efatura.pdf",
            'res_model': 'account.move',
            'res_id': self.id,
            'raw': pdf_content,
            'mimetype': 'application/pdf',
        })
        self.message_main_attachment_id = attachment
        self.with_context(no_new_invoice=True).message_post(
            body=_("GİB e-Fatura görüntüsü Sovos'tan alındı."),
            attachment_ids=attachment.ids,
        )

    def action_l10n_tr_sovos_fetch_pdf(self):
        company = self._l10n_tr_sovos_check_company()
        with _get_sovos_client(company) as client:
            for move in self.filtered('l10n_tr_nilvera_uuid'):
                try:
                    move._l10n_tr_sovos_add_pdf(client)
                except SovosError as e:
                    raise UserError(_("Sovos'tan PDF alınamadı: %s", e)) from e

    # -------------------------------------------------------------------------
    # GELEN FATURALAR
    # -------------------------------------------------------------------------

    @api.model
    def _l10n_tr_sovos_fetch_incoming_invoices(self):
        """ Mevcut şirketin Sovos posta kutusuna gelen faturaları alıp taslak tedarikçi faturası oluşturur. """
        company = self.env.company
        if not company._l10n_tr_sovos_is_active() or not company.l10n_tr_sovos_pk_alias:
            return self.env['account.move']

        vkn = company._l10n_tr_sovos_vkn()
        now = fields.Datetime.now()
        from_date = (company.l10n_tr_sovos_last_fetch_date or now - timedelta(days=30)) - timedelta(days=1)
        date_format = '%Y-%m-%dT%H:%M:%S+00:00'

        journal = company.l10n_tr_sovos_purchase_journal_id or self.env['account.journal'].search([
            *self.env['account.journal']._check_company_domain(company),
            ('type', '=', 'purchase'),
        ], limit=1)

        moves = self.env['account.move']
        with _get_sovos_client(company) as client:
            documents = client.get_ubl_list(
                identifier=company.l10n_tr_sovos_pk_alias,
                vkn_tckn=vkn,
                doc_type='INVOICE',
                type='INBOUND',
                from_date=from_date.strftime(date_format),
                to_date=now.strftime(date_format),
            )
            documents = {doc['UUID']: doc for doc in documents if doc.get('UUID')}
            existing_uuids = set(self.search([
                ('company_id', '=', company.id),
                ('l10n_tr_nilvera_uuid', 'in', list(documents)),
            ]).mapped('l10n_tr_nilvera_uuid'))

            for document_uuid, document in documents.items():
                if document_uuid in existing_uuids:
                    continue
                try:
                    xml_contents = client.get_ubl(
                        identifier=company.l10n_tr_sovos_pk_alias,
                        vkn_tckn=vkn,
                        uuid=document_uuid,
                        doc_type='INVOICE',
                        type='INBOUND',
                    )
                except SovosError as e:
                    _logger.warning("Sovos gelen fatura alınamadı (%s): %s", document_uuid, e)
                    continue
                if not xml_contents:
                    continue
                move = self._l10n_tr_sovos_create_incoming_move(journal, document, xml_contents[0])
                try:
                    move._l10n_tr_sovos_add_pdf(client)
                except SovosError as e:
                    _logger.warning("Sovos gelen fatura PDF'i alınamadı (%s): %s", document_uuid, e)
                moves |= move
                if self.env['account.move.send']._can_commit():
                    self.env.cr.commit()

        company.l10n_tr_sovos_last_fetch_date = now
        if moves:
            journal._notify_einvoices_received(moves)
        return moves

    @api.model
    def _l10n_tr_sovos_create_incoming_move(self, journal, document, xml_content):
        document_uuid = document['UUID']
        profile_id = False
        try:
            tree = etree.fromstring(xml_content)
            profile_id = tree.findtext('{*}ProfileID')
        except etree.XMLSyntaxError:
            _logger.warning("Sovos gelen fatura XML'i okunamadı: %s", document_uuid)

        attachment = self.env['ir.attachment'].create({
            'name': f"{document.get('ID') or document_uuid}.xml",
            'raw': xml_content,
            'type': 'binary',
            'mimetype': 'application/xml',
        })
        sovos_vals = {
            'l10n_tr_nilvera_uuid': document_uuid,
            'l10n_tr_sovos_state': 'received',
            'l10n_tr_sovos_envelope_uuid': document.get('EnvUUID'),
            'l10n_tr_sovos_document_id': document.get('ID'),
            'l10n_tr_sovos_sender_alias': document.get('Identifier'),
            'l10n_tr_sovos_profile_id': profile_id,
        }
        try:
            move = journal.with_context(
                default_move_type='in_invoice',
                default_message_main_attachment_id=attachment.id,
            )._create_document_from_attachment(attachment.id)
            move.write(sovos_vals)
            move._message_log(body=_("e-Fatura Sovos'tan alındı."))
        except Exception:  # noqa: BLE001
            # UBL okunamazsa en azından ekli boş bir taslak fatura oluştur.
            _logger.exception("Sovos gelen fatura içe aktarılamadı: %s", document_uuid)
            move = self.create({
                'move_type': 'in_invoice',
                'journal_id': journal.id,
                'company_id': journal.company_id.id,
                'message_main_attachment_id': attachment.id,
                **sovos_vals,
            })
            attachment.write({'res_model': 'account.move', 'res_id': move.id})
        return move

    # -------------------------------------------------------------------------
    # UYGULAMA YANITI (TİCARİ FATURA KABUL / RED)
    # -------------------------------------------------------------------------

    def _l10n_tr_sovos_get_party_node(self, tag, partner):
        vkn = ''.join(filter(str.isdigit, partner.vat or ''))
        party = etree.Element(etree.QName(NS_CAC, tag))
        identification = etree.SubElement(party, etree.QName(NS_CAC, 'PartyIdentification'))
        etree.SubElement(
            identification, etree.QName(NS_CBC, 'ID'),
            schemeID='TCKN' if len(vkn) == 11 else 'VKN',
        ).text = vkn
        party_name = etree.SubElement(party, etree.QName(NS_CAC, 'PartyName'))
        etree.SubElement(party_name, etree.QName(NS_CBC, 'Name')).text = partner.commercial_partner_id.name
        address = etree.SubElement(party, etree.QName(NS_CAC, 'PostalAddress'))
        if partner.street:
            etree.SubElement(address, etree.QName(NS_CBC, 'StreetName')).text = partner.street
        etree.SubElement(address, etree.QName(NS_CBC, 'CitySubdivisionName')).text = partner.city or partner.state_id.name or '-'
        etree.SubElement(address, etree.QName(NS_CBC, 'CityName')).text = partner.state_id.name or partner.city or '-'
        country = etree.SubElement(address, etree.QName(NS_CAC, 'Country'))
        etree.SubElement(country, etree.QName(NS_CBC, 'Name')).text = partner.country_id.name or 'Türkiye'
        tax_office = getattr(partner, 'l10n_tr_tax_office_id', False)
        tax_office_name = tax_office.name if tax_office else partner.ref
        if tax_office_name:
            tax_scheme = etree.SubElement(etree.SubElement(party, etree.QName(NS_CAC, 'PartyTaxScheme')), etree.QName(NS_CAC, 'TaxScheme'))
            etree.SubElement(tax_scheme, etree.QName(NS_CBC, 'Name')).text = tax_office_name
        return party

    def _l10n_tr_sovos_build_application_response(self, response_code, note, response_uuid):
        self.ensure_one()
        now = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        root = etree.Element(etree.QName(NS_UBL_APP_RESP, 'ApplicationResponse'), nsmap={
            None: NS_UBL_APP_RESP, 'cac': NS_CAC, 'cbc': NS_CBC,
        })
        for tag, text in (
            ('UBLVersionID', '2.1'),
            ('CustomizationID', 'TR1.2'),
            ('ProfileID', 'TICARIFATURA'),
            ('ID', response_uuid),
            ('UUID', response_uuid),
            ('IssueDate', now.strftime('%Y-%m-%d')),
            ('IssueTime', now.strftime('%H:%M:%S')),
        ):
            etree.SubElement(root, etree.QName(NS_CBC, tag)).text = text
        if note:
            etree.SubElement(root, etree.QName(NS_CBC, 'Note')).text = note

        root.append(self._l10n_tr_sovos_get_party_node('SenderParty', self.company_id.partner_id))
        root.append(self._l10n_tr_sovos_get_party_node('ReceiverParty', self.commercial_partner_id))

        document_response = etree.SubElement(root, etree.QName(NS_CAC, 'DocumentResponse'))
        response = etree.SubElement(document_response, etree.QName(NS_CAC, 'Response'))
        etree.SubElement(response, etree.QName(NS_CBC, 'ReferenceID')).text = self.l10n_tr_nilvera_uuid
        etree.SubElement(response, etree.QName(NS_CBC, 'ResponseCode')).text = response_code
        if note:
            etree.SubElement(response, etree.QName(NS_CBC, 'Description')).text = note
        reference = etree.SubElement(document_response, etree.QName(NS_CAC, 'DocumentReference'))
        etree.SubElement(reference, etree.QName(NS_CBC, 'ID')).text = self.l10n_tr_nilvera_uuid
        etree.SubElement(reference, etree.QName(NS_CBC, 'IssueDate')).text = str(self.invoice_date or fields.Date.context_today(self))
        etree.SubElement(reference, etree.QName(NS_CBC, 'DocumentTypeCode')).text = 'FATURA'
        etree.SubElement(reference, etree.QName(NS_CBC, 'DocumentType')).text = 'FATURA'
        return etree.tostring(root, xml_declaration=True, encoding='UTF-8')

    def _l10n_tr_sovos_send_application_response(self, response_code, note=None):
        self.ensure_one()
        if self.l10n_tr_sovos_state != 'received' or self.l10n_tr_sovos_profile_id != 'TICARIFATURA':
            raise UserError(_("Yalnızca Sovos'tan alınan ticari faturalara yanıt verilebilir."))
        if self.l10n_tr_sovos_response:
            raise UserError(_("Bu faturaya zaten yanıt verilmiş."))
        if not self.l10n_tr_sovos_sender_alias:
            raise UserError(_("Faturayı gönderen tarafın etiketi bilinmiyor."))
        company = self._l10n_tr_sovos_check_company()

        response_uuid = str(uuid.uuid4())
        xml_content = self._l10n_tr_sovos_build_application_response(response_code, note, response_uuid)
        try:
            with _get_sovos_client(company) as client:
                client.send_ubl(
                    vkn_tckn=company._l10n_tr_sovos_vkn(),
                    sender_identifier=company.l10n_tr_sovos_pk_alias,
                    receiver_identifier=self.l10n_tr_sovos_sender_alias,
                    document_uuid=response_uuid,
                    xml_content=xml_content,
                    doc_type='APP_RESP',
                )
        except SovosError as e:
            raise UserError(_("Uygulama yanıtı gönderilemedi: %s", e)) from e

        self.write({'l10n_tr_sovos_response': response_code, 'l10n_tr_sovos_response_note': note})
        attachment = self.env['ir.attachment'].create({
            'name': f'{response_uuid}.xml',
            'res_model': 'account.move',
            'res_id': self.id,
            'raw': xml_content,
            'mimetype': 'application/xml',
        })
        self.message_post(
            body=_("Ticari faturaya %(code)s yanıtı gönderildi. %(note)s", code=response_code, note=note or ''),
            attachment_ids=attachment.ids,
        )

    def action_l10n_tr_sovos_open_app_response_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Ticari Fatura Yanıtı"),
            'res_model': 'l10n_tr.sovos.app.response.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_move_id': self.id},
        }

    # -------------------------------------------------------------------------
    # CRONS
    # -------------------------------------------------------------------------

    @api.model
    def _cron_l10n_tr_sovos_update_status(self):
        self.search([
            ('l10n_tr_sovos_state', '=', 'sent'),
            ('move_type', '=', 'out_invoice'),
        ])._l10n_tr_sovos_update_status()

        # Ticari faturalarda alıcı yanıtı 8 gün içinde gelir.
        self.search([
            ('l10n_tr_sovos_state', '=', 'succeed'),
            ('move_type', '=', 'out_invoice'),
            ('l10n_tr_gib_invoice_scenario', '=', 'TICARIFATURA'),
            ('l10n_tr_sovos_response', '=', False),
            ('invoice_date', '>=', fields.Date.context_today(self) - timedelta(days=15)),
        ])._l10n_tr_sovos_update_status()

    @api.model
    def _cron_l10n_tr_sovos_fetch_incoming_invoices(self):
        for company in self.env.companies:
            if company.country_code != 'TR' or not company._l10n_tr_sovos_is_active():
                continue
            try:
                self.with_company(company)._l10n_tr_sovos_fetch_incoming_invoices()
            except SovosError as e:
                _logger.warning("Sovos gelen fatura sorgusu başarısız (%s): %s", company.name, e)
