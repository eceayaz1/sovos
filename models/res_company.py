import re

from odoo import fields, models


class ResCompany(models.Model):
    _inherit = 'res.company'

    l10n_tr_sovos_username = fields.Char(string="Sovos Kullanıcı Adı", groups='base.group_system')
    l10n_tr_sovos_password = fields.Char(string="Sovos Şifre", groups='base.group_system')
    l10n_tr_sovos_use_test_env = fields.Boolean(string="Sovos Test Ortamı", default=True)
    l10n_tr_sovos_gb_alias = fields.Char(
        string="Gönderici Birim Etiketi (GB)",
        help="Faturaları gönderirken kullanılan GİB gönderici birim etiketi, örn. urn:mail:defaultgb@firma.com.tr",
    )
    l10n_tr_sovos_pk_alias = fields.Char(
        string="Posta Kutusu Etiketi (PK)",
        help="Gelen faturaların alındığı GİB posta kutusu etiketi, örn. urn:mail:defaultpk@firma.com.tr",
    )
    l10n_tr_sovos_purchase_journal_id = fields.Many2one(
        comodel_name='account.journal',
        string="Sovos Gelen Fatura Yevmiyesi",
        domain="[('type', '=', 'purchase'), ('company_id', '=', id)]",
    )
    l10n_tr_sovos_last_fetch_date = fields.Datetime(string="Sovos Son Gelen Fatura Sorgusu", readonly=True)

    def _l10n_tr_sovos_is_active(self):
        self.ensure_one()
        company = self.sudo()
        return bool(company.l10n_tr_sovos_username and company.l10n_tr_sovos_password)

    def _l10n_tr_sovos_vkn(self):
        """ Şirket VKN/TCKN'si; 'TR' öneki ve boşluklar temizlenmiş halde. """
        self.ensure_one()
        return re.sub(r'\D', '', self.vat or '')
