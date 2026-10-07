import logging
import re

from odoo import fields, models

from odoo.addons.l10n_tr_sovos_einvoice.lib.sovos_client import SovosError, _get_sovos_client

_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = 'res.partner'

    # l10n_tr_nilvera alanları GİB mükellef durumunu tuttuğu için Sovos ile de aynen kullanılır.
    l10n_tr_nilvera_customer_status = fields.Selection(string="GİB e-Fatura Durumu")
    l10n_tr_nilvera_customer_alias_id = fields.Many2one(string="e-Fatura Etiketi (PK)")

    def _check_nilvera_customer(self):
        # EXTENDS l10n_tr_nilvera
        # Şirkette Sovos tanımlıysa mükellef sorgusu Nilvera yerine Sovos'tan yapılır.
        self.ensure_one()
        if not self.env.company._l10n_tr_sovos_is_active():
            return super()._check_nilvera_customer()
        return self._l10n_tr_sovos_check_customer()

    def _l10n_tr_sovos_check_customer(self):
        self.ensure_one()
        vkn = re.sub(r'\D', '', self.vat or '')
        if not vkn:
            return False

        company = self.env.company
        try:
            with _get_sovos_client(company) as client:
                users = client.get_user_list(
                    identifier=company.l10n_tr_sovos_gb_alias,
                    vkn_tckn=company._l10n_tr_sovos_vkn(),
                    role='PK',
                    filter_vkn_tckn=vkn,
                )
        except SovosError as e:
            _logger.warning("Sovos mükellef sorgusu başarısız (%s): %s", self.display_name, e)
            return False

        aliases = {user.get('Alias') for user in users if user.get('Alias')}
        if not aliases:
            self.l10n_tr_nilvera_customer_status = 'earchive'
            self.l10n_tr_nilvera_customer_alias_id = False
            return True

        self.l10n_tr_nilvera_customer_status = 'einvoice'
        persisted_aliases = self.l10n_tr_nilvera_customer_alias_ids
        persisted_names = set(persisted_aliases.mapped('name'))
        new_aliases = self.env['l10n_tr.nilvera.alias'].create([
            {'name': alias_name, 'partner_id': self.id}
            for alias_name in sorted(aliases - persisted_names)
        ])
        to_keep = persisted_aliases.filtered(lambda a: a.name in aliases)
        (persisted_aliases - to_keep).unlink()

        remaining_aliases = new_aliases | to_keep
        if not self.l10n_tr_nilvera_customer_alias_id and remaining_aliases:
            self.l10n_tr_nilvera_customer_alias_id = remaining_aliases[0]
        return True
