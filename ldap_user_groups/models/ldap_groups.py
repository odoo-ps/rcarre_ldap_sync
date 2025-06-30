from odoo import models, fields, api, Command
import json
from base64 import b64decode
from ldap3 import Server, Connection, ALL
from datetime import datetime
import logging

# Pour afficher les logs
_logger = logging.getLogger(__name__)

# la classe ResUsers est étendue pour ajouter un champ is_ldap_user
class ResUsers(models.Model):
    _inherit = 'res.users'
    is_ldap_user = fields.Boolean(string="Utilisateur LDAP")


# La classe LdapGroup est créée pour gérer les groupes LDAP importés
class LdapGroup(models.Model):
    _name = 'ldap.group'
    _description = 'Groupe LDAP importé'

    name = fields.Char('Nom du Groupe')
    member_names = fields.Text('Utilisateurs')
    last_import_date = fields.Datetime('Dernier import')
    group_ids = fields.Many2many(
        'res.groups',
        string='Groupes Odoo',
        help="Associer ce groupe LDAP à des groupes Odoo pour gérer les droits d'accès."
    )
    stock_data = fields.Binary('Stockage de données', help="Stocker des données JSON")
    import_filename = fields.Char("Nom du fichier")
    linked_ldap_group_id = fields.Many2one(
        'ldap.group',
        string="Groupe LDAP lié",
        help="Permet de sélectionner un autre groupe LDAP pour hériter de ses associations aux groupes Odoo."
    )
    test_login = fields.Char("Login à tester (bascule)")

 # Surcharge de la méthode create pour initialiser un groupe LDAP avec les droits hérités du groupe lié
    @api.model_create_multi
    def create(self, valslist):
        for vals in valslist:
            linked_group_id = vals.get('linked_ldap_group_id')
            if linked_group_id:
                inherited_ids = self._retrieve_group_ids_to_link([linked_group_id])

                # Fusionner avec les group_ids éventuellement présents
                existing_ids = set()
                for cmd in vals.get('group_ids', []):
                    if cmd[0] == Command.set:
                        existing_ids.update(cmd[2])

                vals['group_ids'] = [Command.set(list(existing_ids.union(inherited_ids)))]

        return super().create(valslist)

    # Surcharge de la méthode write pour mettre à jour dynamiquement les droits lors du changement de groupe LDAP lié
    def write(self, vals):
        if 'linked_ldap_group_id' in vals:
            for record in self:
                old_linked_groups = record.linked_ldap_group_id.group_ids if record.linked_ldap_group_id else self.env['res.groups']
                old_group_ids_to_remove = set(old_linked_groups.ids)

                # Si 'group_ids' est déjà dans vals, extraire et fusionner
                existing_vals_group_ids = set()
                if 'group_ids' in vals:
                    for command in vals['group_ids']:
                        if command[0] == Command.set:
                            existing_vals_group_ids.update(command[2])
                    final_group_ids = existing_vals_group_ids - old_group_ids_to_remove
                else:
                    final_group_ids = set(record.group_ids.ids) - old_group_ids_to_remove

                vals['group_ids'] = [Command.set(list(final_group_ids))]

        res = super().write(vals)

        if 'linked_ldap_group_id' in vals:
            # Mise à jour des nouveaux droits hérités
            new_group_ids = self._retrieve_group_ids_to_link(self.ids)
            for group in self:
                group.group_ids = [Command.set(new_group_ids)]

        return res

    # Méthode utilitaire pour mettre à jour le champ group_ids avec les groupes du groupe LDAP lié
    @api.model
    def _retrieve_group_ids_to_link(self, group_ids):
        ldap_group_ids = self.env["ldap.group"].browse(group_ids)
        ids_to_link = set()
        for ldap_group in ldap_group_ids.filtered("linked_ldap_group_id"):
           ids_to_link.update(ldap_group.linked_ldap_group_id.group_ids.ids)
        return list(ids_to_link)


# Cette méthode permet d'importer les groupes LDAP et de mettre à jour les utilisateurs Odoo
    @api.model
    def import_ldap_groups(self, *args, **kwargs):
        """
        N'importe que les groupes LDAP et leurs membres depuis l'annuaire,
        sans appliquer de droits dans Odoo.
        """
        config = self.env['ir.config_parameter'].sudo()
        ldap_ip = config.get_param('ldap_user_groups.ldap_ip')
        ldap_user = config.get_param('ldap_user_groups.ldap_user')
        ldap_mdp = config.get_param('ldap_user_groups.ldap_mdp')
        ldap_DN = config.get_param('ldap_user_groups.ldap_DN')

        if not all([ldap_ip, ldap_user, ldap_mdp, ldap_DN]):
            raise ValueError("Paramètres LDAP manquants.")

        server = Server(ldap_ip, get_info=ALL)
        connexion = Connection(server, user=ldap_user, password=ldap_mdp, auto_bind=True)

        connexion.search(
            search_base=ldap_DN,
            search_filter='(&(ObjectClass=group)(cn=*))',
            attributes=['cn', 'member'],
        )

        for entry in connexion.entries:
            if not entry:
                continue

            group_name = entry.cn.value
            members_list = []

            if 'member' in entry:
                for dn in entry.member.values:
                    if connexion.search(dn, '(&(ObjectClass=person))', attributes=['cn']):
                        cn_val = connexion.entries[0].cn.value
                        if cn_val:
                            members_list.append(cn_val)

            # Mise à jour ou création du groupe LDAP
            existing_group = self.env['ldap.group'].search([('name', '=', group_name)], limit=1)
            vals = {
                'member_names': '\n'.join(members_list),
                'last_import_date': datetime.now(),
            }

            if existing_group:
                existing_group.write(vals)
            else:
                vals['name'] = group_name
                self.env['ldap.group'].create(vals)



# Cette méthode permet d'importer les droits depuis un fichier JSON
    def import_odoo_permissions_from_json(self):
        if not self.stock_data:
            raise ValueError("Aucun fichier JSON n'a été importé.")

        try:
            json_data = b64decode(self.stock_data)
            droits_map = json.loads(json_data)
        except Exception as e:
            raise ValueError(f"Erreur lors du décodage JSON : {str(e)}")

        user_to_groups = {}

        # Précharger tous les groupes Odoo pour éviter les recherches répétées
        all_groups = self.env['res.groups'].search([])
        all_users = self.env['res.users'].search([])

        for ldap_name, droits in droits_map.items():
            _logger.info(f"[IMPORT DROITS] Groupe LDAP : '{ldap_name}' - Groupes demandés : {droits}")

            ldap_group = self.search([('name', '=', ldap_name)], limit=1)
            if not ldap_group:
                _logger.warning(f"Groupe LDAP '{ldap_name}' introuvable.")
                continue

            groupes_ids = set()

            for droit in droits:
                droit = droit.strip()
                if '/' in droit:
                    category, group_name = map(str.strip, droit.split('/', 1))
                    group = all_groups.filtered(lambda g: g.name.casefold() == group_name.casefold() and g.category_id.name.casefold() == category.casefold())
                else:
                    group = all_groups.filtered(lambda g: g.name.casefold() == droit.casefold())

                if group:
                    groupes_ids.add(group.id)
                else:
                    _logger.warning(f"[WARN] Groupe Odoo non trouvé pour droit : '{droit}'")

            # Ajouter les droits hérités si groupe lié
            if ldap_group.linked_ldap_group_id:
                groupes_ids.update(ldap_group.linked_ldap_group_id.group_ids.ids)

            # Mise à jour du groupe LDAP
            ldap_group.group_ids = [Command.set(list(groupes_ids))]

            # Accumuler les droits pour chaque utilisateur membre
            for user_name in filter(None, map(str.strip, ldap_group.member_names.split('\n'))):
                user = all_users.filtered(lambda u: u.partner_id.name.casefold() == user_name.casefold())
                if user:
                    user_to_groups.setdefault(user.id, set()).update(groupes_ids)

        # Mise à jour des utilisateurs
        for user_id, group_ids in user_to_groups.items():
            user = self.env['res.users'].browse(user_id)
            if user:
                user.write({
                    'groups_id': [
                        # Réinitialiser tous les groupes
                        Command.clear()
                        ] + [
                        # Réaffecter les groupes LDAP associés
                        Command.link(gid) for gid in group_ids
                    ],
                    'is_ldap_user': True,
                    })

            else:
                _logger.info(f"[INFO] Utilisateur LDAP ID '{user_id}' introuvable dans Odoo. Aucun droit appliqué.")
            
    
    # Cette méthode bascule les utilisateurs connectés vers le groupe "Utilisateur interne"  
    def switch_connected_users(self, *args, **kwargs):
        internal_group = self.env.ref("base.group_user", raise_if_not_found=False)

        if not internal_group:
            _logger.warning("Le groupe 'Utilisateur interne' est introuvable.")

        portal_group = self.env.ref('base.group_portal', raise_if_not_found=False)
        public_group = self.env.ref('base.group_public', raise_if_not_found=False)

        # Récupération des utilisateurs ayant déjà une connexion
        self.env.cr.execute("""
            SELECT DISTINCT ru.id
            FROM res_users_log rul
            JOIN res_users ru ON ru.id = rul.create_uid
        """)
        user_ids = [row[0] for row in self.env.cr.fetchall() if row and row[0]]
        users = self.env['res.users'].browse(user_ids)

        # Charger le groupe LDAP RC_ODOO_Login-Prod
        groupe_ldap = self.env['ldap.group'].search([('name', '=', 'RC_ODOO_Login-Prod')], limit=1)
        membres_autorises = set()
        if groupe_ldap:
            membres_autorises = set(map(str.strip, groupe_ldap.member_names.splitlines()))

        for user in users:
            is_portal_or_public = (
                (portal_group and portal_group.id in user.groups_id.ids) or
                (public_group and public_group.id in user.groups_id.ids)
            )
            is_member_of_rc_odoo = user.partner_id.name in membres_autorises

            _logger.info(f"[CHECK] {user.login} - portal/public: {is_portal_or_public}, RC_ODOO_Login-Prod: {is_member_of_rc_odoo}")

            if is_portal_or_public and is_member_of_rc_odoo:
                user.write({
                    'groups_id': [Command.clear(), Command.link(internal_group.id)],
                    'is_ldap_user': True
                })
                _logger.info(f"[INFO] Utilisateur {user.login} basculé en interne.")
            else:
                _logger.info(f"[SKIP] Utilisateur {user.login} ne remplit pas toutes les conditions.")
