# -*- coding: utf-8 -*-
{
    'name': "ldap_user_groups",

    'summary': "Afficher les groupes LDAP",

    'description': """
Long description of module's purpose
    """,

    'author': "My Company",
    'website': "https://www.yourcompany.com",

    # Categories can be used to filter modules in modules listing
    # Check https://github.com/odoo/odoo/blob/15.0/odoo/addons/base/data/ir_module_category_data.xml
    # for the full list
    'category': 'Uncategorized',
    'version': '0.1',

    # any module necessary for this one to work correctly
    'depends': ['base'],

    # always loaded
    'data': [
        'security/ir.model.access.csv',
        'views/views.xml',
    ],
    
    'external_dependencies': {
        'python': ['ldap3'],
    },
    
    
    'license': 'LGPL-3',
    'application': True,
    'installable': True,
}

