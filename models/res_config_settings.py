from odoo import _, fields, models
from odoo.exceptions import UserError

from odoo.addons.l10n_tr_sovos_einvoice.lib.sovos_client import SovosError, _get_sovos_client


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    l10n_tr_sovos_username = fields.Char(related='company_id.l10n_tr_sovos_username', readonly=False)
    l10n_tr_sovos_password = fields.Char(related='company_id.l10n_tr_sovos_password', readonly=False)
    l10n_tr_sovos_use_test_env = fields.Boolean(related='company_id.l10n_tr_sovos_use_test_env', readonly=False)
    l10n_tr_sovos_gb_alias = fields.Char(related='company_id.l10n_tr_sovos_gb_alias', readonly=False)
    l10n_tr_sovos_pk_alias = fields.Char(related='company_id.l10n_tr_sovos_pk_alias', readonly=False)
    l10n_tr_sovos_purchase_journal_id = fields.Many2one(related='company_id.l10n_tr_sovos_purchase_journal_id', readonly=False)
    l10n_tr_sovos_last_fetch_date = fields.Datetime(related='company_id.l10n_tr_sovos_last_fetch_date')

    def _l10n_tr_sovos_notify(self, message, notification_type='success'):
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {'message': message, 'type': notification_type, 'sticky': False},
        }

    def action_l10n_tr_sovos_test_connection(self):
        """ Bağlantıyı test eder ve şirketin kendi GB/PK etiketlerini Sovos'tan çekerek boş olanları doldurur. """
        self.ensure_one()
        company = self.company_id
        vkn = company._l10n_tr_sovos_vkn()
        if not vkn:
            raise UserError(_("Şirketin Vergi Numarası (VKN/TCKN) tanımlı değil."))
        try:
            with _get_sovos_client(company) as client:
                aliases = {}
                for role in ('GB', 'PK'):
                    users = client.get_user_list(
                        identifier=company.l10n_tr_sovos_gb_alias or company.l10n_tr_sovos_pk_alias,
                        vkn_tckn=vkn,
                        role=role,
                        filter_vkn_tckn=vkn,
                    )
                    aliases[role] = [user.get('Alias') for user in users if user.get('Alias')]
        except SovosError as e:
            raise UserError(_("Sovos bağlantısı başarısız: %s", e)) from e

        if not company.l10n_tr_sovos_gb_alias and aliases['GB']:
            company.l10n_tr_sovos_gb_alias = aliases['GB'][0]
        if not company.l10n_tr_sovos_pk_alias and aliases['PK']:
            company.l10n_tr_sovos_pk_alias = aliases['PK'][0]

        message = _(
            "Sovos bağlantısı başarılı.\nGB etiketleri: %(gb)s\nPK etiketleri: %(pk)s",
            gb=', '.join(aliases['GB']) or '-',
            pk=', '.join(aliases['PK']) or '-',
        )
        return self._l10n_tr_sovos_notify(message)

    def action_l10n_tr_sovos_fetch_incoming(self):
        self.ensure_one()
        moves = self.env['account.move'].with_company(self.company_id)._l10n_tr_sovos_fetch_incoming_invoices()
        return self._l10n_tr_sovos_notify(_("Sovos'tan %s yeni gelen fatura alındı.", len(moves)))
