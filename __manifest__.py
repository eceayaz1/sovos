{
    'name': 'Türkiye - Sovos e-Fatura',
    'version': '19.0.1.0.0',
    'category': 'Accounting/Localizations/EDI',
    'summary': "Sovos (FIT) e-Fatura web servisi ile fatura gönderme, alma, durum sorgulama ve ticari fatura yanıtı",
    'description': """
Sovos e-Fatura Entegrasyonu
===========================

ahmeti/sovos-api kütüphanesindeki e-Fatura servisinin (ClientEInvoiceServices) Odoo 19 karşılığı.
UBL-TR 1.2 belgesi Odoo'nun Türkiye yerelleştirmesi (l10n_tr_nilvera_einvoice_extended) ile üretilir;
taşıma katmanı olarak Nilvera yerine Sovos SOAP servisi kullanılır.

* sendUBL: Faturayı "Gönder ve Yazdır" sihirbazından "Sovos e-Fatura" seçeneğiyle gönderme
* getUserList: İş ortağının e-Fatura mükellefiyetini ve PK etiketini sorgulama
* getEnvelopeStatus: Zarf durumunu (GİB yanıtı) zamanlanmış görevle takip etme
* getInvResponses: Ticari faturaya alıcının verdiği KABUL / RED yanıtını alma
* getUBLList + getUBL: Gelen faturaları taslak tedarikçi faturası olarak içe aktarma
* getInvoiceView: GİB fatura görüntüsünü (PDF) faturaya ekleme
* sendUBL (APP_RESP): Gelen ticari faturalara KABUL / RED uygulama yanıtı gönderme
* getRAWUserList: Tüm kayıtlı kullanıcı listesi (istemci kütüphanesinde)
    """,
    'author': 'Coflow Teknoloji',
    'website': 'https://coflow.com.tr',
    'license': 'LGPL-3',
    'depends': ['l10n_tr_nilvera_einvoice_extended'],
    'external_dependencies': {'python': ['requests', 'lxml']},
    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron.xml',
        'views/res_config_settings_views.xml',
        'views/account_move_views.xml',
        'wizard/sovos_app_response_wizard_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}
