import re

from odoo import _, api, models

from odoo.addons.l10n_tr_sovos_einvoice.lib.sovos_client import SovosError


class AccountMoveSend(models.AbstractModel):
    _inherit = 'account.move.send'

    @api.model
    def _is_tr_sovos_applicable(self, move):
        return (
            move.move_type == 'out_invoice'
            and move.country_code == 'TR'
            and move.l10n_tr_sovos_state == 'not_sent'
            and move.company_id._l10n_tr_sovos_is_active()
            and move.partner_id.commercial_partner_id.l10n_tr_nilvera_customer_status in ('einvoice', 'not_checked')
        )

    @api.model
    def _is_tr_nilvera_applicable(self, move):
        # EXTENDS l10n_tr_nilvera_einvoice
        # Sovos kullanan şirkette Nilvera ile gönderim seçeneği gösterilmez.
        if move.company_id._l10n_tr_sovos_is_active():
            return False
        return super()._is_tr_nilvera_applicable(move)

    def _get_all_extra_edis(self) -> dict:
        # EXTENDS 'account'
        res = super()._get_all_extra_edis()
        res.update({'tr_sovos': {'label': _("Sovos e-Fatura"), 'is_applicable': self._is_tr_sovos_applicable}})
        return res

    # -------------------------------------------------------------------------
    # ALERTS
    # -------------------------------------------------------------------------

    def _get_alerts(self, moves, moves_data):
        def _is_valid_gib_name(move):
            # GİB fatura no formatı: 3 karakter seri + 4 haneli yıl + 9 haneli sıra (ABC2026000000001)
            _dummy, parts = move._get_sequence_format_param(move.name)
            return (
                parts['year'] != 0
                and parts['year_length'] == 4
                and parts['seq'] != 0
                and re.match(r'^[A-Za-z0-9]{3}[^A-Za-z0-9]?$', parts['prefix1'])
            )

        alerts = super()._get_alerts(moves, moves_data)
        if self.env.company._l10n_tr_sovos_is_active():
            alerts.pop('l10n_tr_nilvera_einvoice_test_mode', None)
        tr_sovos_moves = moves.filtered(lambda m: 'tr_sovos' in moves_data[m]['extra_edis'])
        if not tr_sovos_moves:
            return alerts

        if tr_sovos_moves.company_id.filtered('l10n_tr_sovos_use_test_env'):
            alerts['l10n_tr_sovos_test_mode'] = {
                'level': 'info',
                'message': _("Sovos test ortamı etkin."),
            }

        if companies_missing_alias := tr_sovos_moves.company_id.filtered(lambda c: not c.l10n_tr_sovos_gb_alias):
            alerts['tr_sovos_companies_missing_alias'] = {
                'level': 'danger',
                'message': _("Şirket(ler) için Sovos Gönderici Birim (GB) etiketi tanımlı değil."),
                'action_text': _("Ayarlara Git"),
                'action': self.env['ir.actions.actions']._for_xml_id('account.action_account_config'),
            }

        if companies_missing_fields := tr_sovos_moves.company_id.filtered(
            lambda c: not c.vat or not c.street or not c.city or not c.state_id or c.country_code != 'TR'
        ):
            alerts['tr_sovos_companies_missing_fields'] = {
                'level': 'danger',
                'message': _("Şirket(ler)de Vergi No, Adres, Şehir veya İl bilgisi eksik ya da ülke Türkiye değil."),
                'action_text': _("Şirketleri Görüntüle"),
                'action': companies_missing_fields._get_records_action(name=_("Şirket bilgilerini kontrol edin")),
            }

        # l10n_tr_nilvera_einvoice(_extended) içindeki adres / vergi dairesi kontrolleri tekrar kullanılır.
        if partner_address_alert := self._get_l10n_tr_tax_partner_address_alert(tr_sovos_moves):
            alerts['tr_sovos_partners_missing_required_fields'] = partner_address_alert
        if partner_tax_office_alert := self._get_l10n_tr_tax_partner_tax_office_alert(tr_sovos_moves):
            alerts['tr_sovos_partners_missing_tax_office'] = partner_tax_office_alert
        if company_tax_office_alert := self._get_l10n_tr_tax_company_tax_office_alert(tr_sovos_moves):
            alerts['tr_sovos_companies_missing_tax_office'] = company_tax_office_alert

        if partners_not_checked := tr_sovos_moves.partner_id.commercial_partner_id.filtered(
            lambda p: p.l10n_tr_nilvera_customer_status == 'not_checked'
        ):
            alerts['tr_sovos_partners_not_checked'] = {
                'level': 'warning',
                'message': _("Aşağıdaki iş ortaklarının e-Fatura mükellefiyeti gönderim sırasında Sovos'tan sorgulanacak."),
                'action_text': _("İş Ortaklarını Görüntüle"),
                'action': partners_not_checked._get_records_action(name=_("e-Fatura durumunu kontrol edin")),
            }

        if negative_lines := tr_sovos_moves.filtered(lambda m: m._l10n_tr_nilvera_einvoice_check_negative_lines()):
            alerts['critical_tr_sovos_negative_lines'] = {
                'level': 'danger',
                'message': _("e-Faturada negatif miktar veya negatif birim fiyatlı satır olamaz."),
                'action_text': _("Faturaları Görüntüle"),
                'action': negative_lines._get_records_action(name=_("Fatura satırlarını kontrol edin")),
            }

        if invalid_names := tr_sovos_moves.filtered(lambda m: not _is_valid_gib_name(m)):
            alerts['tr_sovos_moves_with_invalid_name'] = {
                'level': 'danger',
                'message': _(
                    "GİB fatura numarası için fatura adı şu formatta olmalıdır: 3 karakter seri, yıl ve sıra no. "
                    "Örnek: INV/2026/000001"
                ),
                'action_text': _("Faturaları Görüntüle"),
                'action': invalid_names._get_records_action(name=_("Fatura numaralarını kontrol edin")),
            }
        return alerts

    # -------------------------------------------------------------------------
    # BUSINESS ACTIONS
    # -------------------------------------------------------------------------

    @api.model
    def _l10n_tr_sovos_get_xml(self, invoice, invoice_data):
        if attachment_values := invoice_data.get('ubl_cii_xml_attachment_values'):
            return attachment_values['raw'], set()
        if invoice.ubl_cii_xml_id:
            return invoice.ubl_cii_xml_id.raw, set()
        return self.env['account.edi.xml.ubl.tr']._export_invoice(invoice)

    @api.model
    def _call_web_service_before_invoice_pdf_render(self, invoices_data):
        # EXTENDS 'account'
        super()._call_web_service_before_invoice_pdf_render(invoices_data)

        for invoice, invoice_data in invoices_data.items():
            if 'tr_sovos' not in invoice_data['extra_edis']:
                continue

            partner = invoice.partner_id.commercial_partner_id
            if not partner.l10n_tr_nilvera_customer_alias_id:
                partner._check_nilvera_customer()
            receiver_alias = invoice._get_partner_l10n_tr_nilvera_customer_alias_name() \
                or partner.l10n_tr_nilvera_customer_alias_id.name
            if partner.l10n_tr_nilvera_customer_status != 'einvoice' or not receiver_alias:
                invoice_data['error'] = {
                    'error_title': _("Sovos e-Fatura gönderilemedi"),
                    'errors': [_("%s e-Fatura mükellefi değil veya posta kutusu etiketi bulunamadı.", partner.display_name)],
                }
                continue

            xml_content, errors = self._l10n_tr_sovos_get_xml(invoice, invoice_data)
            if errors:
                invoice_data['error'] = {
                    'error_title': _("e-Fatura UBL oluşturulurken hata"),
                    'errors': sorted(errors),
                }
                continue

            try:
                invoice._l10n_tr_sovos_send_einvoice(xml_content, receiver_alias)
            except SovosError as e:
                invoice_data['error'] = {
                    'error_title': _("Sovos e-Fatura gönderilemedi"),
                    'errors': [str(e)],
                }
                continue

            if self._can_commit():
                self.env.cr.commit()

    def _link_invoice_documents(self, invoices_data):
        # EXTENDS 'l10n_tr_nilvera_einvoice' (TR şirketlerinde is_move_sent yalnızca Nilvera'ya bakıyordu)
        super()._link_invoice_documents(invoices_data)
        for invoice in invoices_data:
            if invoice.l10n_tr_sovos_state in ('sent', 'succeed'):
                invoice.is_move_sent = True
