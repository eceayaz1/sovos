from odoo import fields, models


class L10nTrSovosAppResponseWizard(models.TransientModel):
    _name = 'l10n_tr.sovos.app.response.wizard'
    _description = "Sovos Ticari Fatura Yanıtı"

    move_id = fields.Many2one('account.move', string="Fatura", required=True, readonly=True)
    response_code = fields.Selection(
        selection=[('KABUL', "Kabul"), ('RED', "Red")],
        string="Yanıt",
        required=True,
        default='KABUL',
    )
    note = fields.Char(string="Açıklama")

    def action_send(self):
        self.ensure_one()
        self.move_id._l10n_tr_sovos_send_application_response(self.response_code, self.note)
        return {'type': 'ir.actions.act_window_close'}
