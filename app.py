from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import mysql.connector
import psycopg2
import sys
import traceback
from dotenv import load_dotenv
import os
import re
from datetime import datetime

# Chargement des variables d'environnement
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'une_cle_secrete_pour_les_sessions')  # Ajout de clé de session

# Liste des bases de données autorisées
ALLOWED_DATABASES = ['THIERNO', 'ASSANE', 'ALPHONSE', 'BBYOU', 'VENTES1']

# Structure connue de la base de données (pour optimiser le mapping des relations)
TABLE_RELATIONS = {
    'COMMANDE': {
        'id_client': {'table': 'CLIENT', 'pk': 'id_client'},
        'id_vendeur': {'table': 'VENDEUR', 'pk': 'id_vendeur'}
    },
    'LIGNE_COMMANDE': {
        'id_commande': {'table': 'COMMANDE', 'pk': 'id_commande'},
        'id_produit': {'table': 'PRODUIT', 'pk': 'id_produit'}
    },
    'AVIS': {
        'id_client': {'table': 'CLIENT', 'pk': 'id_client'},
        'id_produit': {'table': 'PRODUIT', 'pk': 'id_produit'}
    }
}

# Configuration MySQL
def get_mysql_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv('MYSQL_HOST', 'localhost'),
            user=os.getenv('MYSQL_USER', 'root'),
            password=os.getenv('MYSQL_PASSWORD', 'espcae'),
            port=int(os.getenv('MYSQL_PORT', 3306))
        )
        print("Connexion MySQL réussie!")
        return conn
    except Exception as e:
        print(f"Erreur de connexion MySQL: {e}")
        sys.exit(1)

# Configuration PostgreSQL
def get_postgres_connection():
    try:
        conn = psycopg2.connect(
            host=os.getenv('POSTGRES_HOST', 'localhost'),
            user=os.getenv('POSTGRES_USER', 'postgres'),
            password=os.getenv('POSTGRES_PASSWORD', 'espace'),
            port=int(os.getenv('POSTGRES_PORT', 5432)),
            database=os.getenv('POSTGRES_DB', 'central_db')
        )
        print("Connexion PostgreSQL réussie!")
        return conn
    except Exception as e:
        print(f"Erreur de connexion PostgreSQL: {e}")
        sys.exit(1)

# Récupérer la clé primaire automatiquement
def get_primary_keys(cursor, table_name):
    cursor.execute(f"SHOW KEYS FROM `{table_name}` WHERE Key_name = 'PRIMARY'")
    results = cursor.fetchall()
    
    if not results:
        print(f"[AVERTISSEMENT] Aucune clé primaire trouvée pour la table {table_name}")
        return []
    
    if isinstance(results[0], dict):
        return [row['Column_name'] for row in results]
    else:
        column_names = cursor.column_names
        column_name_idx = column_names.index('Column_name') if 'Column_name' in column_names else 3
        return [row[column_name_idx] for row in results]


# Vérifier si la table est vide et réinitialiser la séquence si nécessaire
def check_and_reset_table(pg_cursor, table_name):
    try:
        pg_cursor.execute(f"""
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                               AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = '{table_name.lower()}'::regclass
              AND i.indisprimary;
        """)
        result = pg_cursor.fetchone()
        
        if not result:
            print(f"Aucune clé primaire trouvée pour {table_name}")
            return False
        
        primary_key_column = result[0]
        print(f"Colonne clé primaire détectée: {primary_key_column}")

        # Toujours réinitialiser la séquence à 1, indépendamment du nombre d'entrées
        # pour s'assurer que les IDs commencent toujours à 1
        pg_cursor.execute(f"SELECT pg_get_serial_sequence('{table_name.lower()}', '{primary_key_column}')")
        sequence_name = pg_cursor.fetchone()[0]
        
        if sequence_name:
            pg_cursor.execute(f"ALTER SEQUENCE {sequence_name} RESTART WITH 1")
            print(f"Séquence {sequence_name} réinitialisée à 1 pour {table_name}")
            return True
        else:
            print(f"Aucune séquence trouvée pour {table_name}.{primary_key_column}")
    except Exception as e:
        print(f"Erreur dans check_and_reset_table: {e}")
        raise

    return False

# Fonction pour réinitialiser les compteurs de séquence pour toutes les tables
def reset_all_sequences(pg_cursor):
    try:
        # Récupérer toutes les séquences de la base de données
        pg_cursor.execute("""
            SELECT sequence_name
            FROM information_schema.sequences
            WHERE sequence_schema = 'public'
        """)
        sequences = pg_cursor.fetchall()
        
        for seq in sequences:
            pg_cursor.execute(f"ALTER SEQUENCE {seq[0]} RESTART WITH 1")
            print(f"Séquence {seq[0]} réinitialisée à 1")
        
        return True
    except Exception as e:
        print(f"Erreur dans reset_all_sequences: {e}")
        raise
        
    return False

# Vider la table de mapping pour une table donnée
def clear_mapping_table(pg_cursor, db_name, table_name):
    mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
    
    try:
        pg_cursor.execute(f"""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = %s
            )
        """, (mapping_table,))
        exists = pg_cursor.fetchone()[0]
        
        if exists:
            pg_cursor.execute(f"TRUNCATE TABLE {mapping_table}")
            print(f"Table de mapping {mapping_table} vidée")
        
        return True
    except Exception as e:
        print(f"Erreur lors du nettoyage de la table de mapping: {e}")
        raise
        
    return False

# Créer ou récupérer une table de mapping
def ensure_mapping_table_exists(pg_cursor, db_name, table_name):
    # Nommer la table de mapping spécifiquement pour cette base de données et cette table
    mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
    
    try:
        # Vérifier si la table existe déjà
        pg_cursor.execute(f"""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = %s
            )
        """, (mapping_table,))
        exists = pg_cursor.fetchone()[0]
        
        if not exists:
            # Créer la table si elle n'existe pas
            pg_cursor.execute(f"""
                CREATE TABLE {mapping_table} (
                    id SERIAL PRIMARY KEY,
                    table_name VARCHAR(255) NOT NULL,
                    old_id VARCHAR(255) NOT NULL,
                    new_id VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(table_name, old_id)
                )
            """)
            print(f"Table de mapping créée: {mapping_table}")
        
        return mapping_table
    
    except Exception as e:
        print(f"Erreur lors de la création de la table de mapping: {e}")
        raise

# Fonction pour gérer l'ordre de transfert des tables
def get_table_dependency_order():
    # Définir l'ordre de dépendance pour respecter les contraintes de clés étrangères
    return [
        'CLIENT',
        'PRODUIT',
        'VENDEUR',
        'COMMANDE',
        'LIGNE_COMMANDE',
        'AVIS'
    ]

# Mapper les ID pour les clés étrangères
def map_foreign_key(pg_cursor, db_name, table_name, fk_column, old_id):
    if old_id is None:
        return None
    
    # Déterminer la table référencée basée sur nos connaissances du schéma
    referenced_table = None
    referenced_column = None
    
    # Vérifier dans notre structure connue
    if table_name.upper() in TABLE_RELATIONS and fk_column.lower() in [k.lower() for k in TABLE_RELATIONS[table_name.upper()]]:
        relation_info = TABLE_RELATIONS[table_name.upper()][fk_column]
        referenced_table = relation_info['table']
        referenced_column = relation_info['pk']
    else:
        # Méthode heuristique pour deviner la table référencée
        if fk_column.lower().startswith('id_'):
            # Extraire le nom de table probable à partir du nom de colonne
            potential_table = fk_column[3:].upper()  # Supprime 'id_' et met en majuscule
            referenced_table = potential_table
            referenced_column = fk_column
    
    if referenced_table:
        # Construire le nom de la table de mapping
        mapping_table = f"id_mapping_{db_name.lower()}_{referenced_table.lower()}"
        
        try:
            # Vérifier si la table de mapping existe
            pg_cursor.execute(f"""
                SELECT EXISTS (
                    SELECT FROM information_schema.tables
                    WHERE table_name = %s
                )
            """, (mapping_table,))
            exists = pg_cursor.fetchone()[0]
            
            if exists:
                # Chercher le mapping
                pg_cursor.execute(f"""
                    SELECT new_id 
                    FROM {mapping_table} 
                    WHERE old_id = %s
                """, (str(old_id),))
                result = pg_cursor.fetchone()
                
                if result:
                    return result[0]
                else:
                    print(f"[Avertissement] Aucun mapping trouvé pour {referenced_table}.{referenced_column} = {old_id}")
        except Exception as e:
            print(f"[Erreur mapping FK] {table_name}.{fk_column} -> {e}")
    
    # Si aucun mapping n'est trouvé, retourner la valeur originale
    return old_id

# Nouveau point d'entrée pour réinitialiser les séquences
@app.route('/reset-sequences', methods=['POST'])
def reset_sequences():
    pg_conn = None
    pg_cursor = None
    
    try:
        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()
        
        # Réinitialiser toutes les séquences
        reset_all_sequences(pg_cursor)
        pg_conn.commit()
        
        return jsonify({"success": True, "message": "Toutes les séquences ont été réinitialisées à 1"})
    
    except Exception as e:
        if pg_conn:
            try:
                pg_conn.rollback()
            except:
                pass
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    
    finally:
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()

# Nouveau point d'entrée pour vider une table de mapping
@app.route('/clear-mapping/<db_name>/<table_name>', methods=['POST'])
def clear_mapping(db_name, table_name):
    pg_conn = None
    pg_cursor = None
    
    try:
        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()
        
        # Vider la table de mapping
        clear_mapping_table(pg_cursor, db_name, table_name)
        pg_conn.commit()
        
        return jsonify({"success": True, "message": f"Table de mapping pour {db_name}.{table_name} vidée"})
    
    except Exception as e:
        if pg_conn:
            try:
                pg_conn.rollback()
            except:
                pass
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
    
    finally:
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()

@app.route('/')
def index():
    connection = get_mysql_connection()
    cursor = connection.cursor()
    cursor.execute("SHOW DATABASES")
    databases = [db[0] for db in cursor.fetchall()
                if db[0] in ALLOWED_DATABASES]
    cursor.close()
    connection.close()
    
    # Ajouter l'ordre des tables pour l'affichage dans le template
    table_order = get_table_dependency_order()
    
    return render_template('index.html', 
                          databases=databases, 
                          table_order=table_order)

@app.route('/database/<db_name>')
def show_database(db_name):
    # Mémoriser la base de données actuelle dans la session
    session['current_db'] = db_name
    
    connection = get_mysql_connection()
    connection.database = db_name
    cursor = connection.cursor()
    cursor.execute("SHOW TABLES")
    all_tables = [table[0] for table in cursor.fetchall()]
    
    # Obtenir l'ordre de dépendance des tables
    ordered_tables = get_table_dependency_order()
    
    # Trier les tables selon l'ordre défini (les tables non définies viennent à la fin)
    tables = []
    for table in ordered_tables:
        if table in all_tables:
            tables.append(table)
            all_tables.remove(table)
    
    # Ajouter les tables restantes (non définies dans l'ordre)
    tables.extend(all_tables)
    
    cursor.close()
    connection.close()
    
    return render_template('database.html', db_name=db_name, tables=tables)

@app.route('/database/<db_name>/table/<table_name>')
def show_table(db_name, table_name):
    # Mémoriser la table actuelle dans la session
    session['current_db'] = db_name
    session['current_table'] = table_name
    
    connection = get_mysql_connection()
    connection.database = db_name
    cursor = connection.cursor(dictionary=True)
    
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
    columns = [column['Field'] for column in cursor.fetchall()]
    
    # Obtenir les informations sur les clés primaires
    primary_keys = get_primary_keys(cursor, table_name)
    
    # Obtenir les informations sur les clés étrangères si possible
    foreign_keys = {}
    if table_name.upper() in TABLE_RELATIONS:
        foreign_keys = TABLE_RELATIONS[table_name.upper()]
    
    cursor.execute(f"SELECT * FROM `{table_name}`")
    rows = cursor.fetchall()
    
    # Vérifier également quelles lignes ont déjà été transférées
    mapped_rows = []
    
    try:
        pg_conn = get_postgres_connection() 
        pg_cursor = pg_conn.cursor()
        
        mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
        pg_cursor.execute(f"""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = %s
            )
        """, (mapping_table,))
        
        if pg_cursor.fetchone()[0]:
            # Pour les tables à clé composite (LIGNE_COMMANDE)
            if table_name.upper() == 'LIGNE_COMMANDE':
                for row in rows:
                    # Créer l'ID composite pour la recherche
                    composite_id = f"{row['id_commande']}_{row['id_produit']}"
                    
                    pg_cursor.execute(f"""
                        SELECT COUNT(*) FROM {mapping_table}
                        WHERE old_id = %s
                    """, (composite_id,))
                    
                    if pg_cursor.fetchone()[0] > 0:
                        mapped_rows.append(composite_id)
            else:
                # Pour les tables à clé simple
                for row in rows:
                    if primary_keys and primary_keys[0] in row:
                        pk_value = str(row[primary_keys[0]])
                        
                        pg_cursor.execute(f"""
                            SELECT COUNT(*) FROM {mapping_table}
                            WHERE old_id = %s
                        """, (pk_value,))
                        
                        if pg_cursor.fetchone()[0] > 0:
                            mapped_rows.append(pk_value)
        
        pg_cursor.close()
        pg_conn.close()
    except Exception as e:
        print(f"Erreur lors de la vérification des lignes transférées: {e}")
    
    cursor.close()
    connection.close()
    
    return render_template('table.html', 
                          db_name=db_name, 
                          table_name=table_name, 
                          columns=columns, 
                          rows=rows, 
                          foreign_keys=foreign_keys,
                          mapped_rows=mapped_rows,
                          primary_keys=primary_keys)

@app.route('/transfer', methods=['POST'])
def transfer_row():
    mysql_conn = None
    pg_conn = None
    mysql_cursor = None
    pg_cursor = None

    try:
        db_name = request.form.get('db_name')
        table_name = request.form.get('table_name')
        row_id = request.form.get('row_id')
        id_column = request.form.get('id_column')
        reset_sequence = request.form.get('reset_sequence', 'false').lower() == 'true'

        # Connexions
        mysql_conn = get_mysql_connection()
        mysql_conn.database = db_name
        mysql_cursor = mysql_conn.cursor(dictionary=True)

        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()

        # Récupération des clés primaires
        primary_keys = get_primary_keys(mysql_cursor, table_name)
        is_composite_key_table = len(primary_keys) > 1

        # Si réinitialisation demandée, vider la table de destination et réinitialiser les séquences
        if reset_sequence:
            # Vider la table PostgreSQL
            pg_cursor.execute(f"TRUNCATE TABLE {table_name.lower()} RESTART IDENTITY CASCADE")
            
            # Vider la table de mapping correspondante
            clear_mapping_table(pg_cursor, db_name, table_name)
            
            # Réinitialiser les séquences
            check_and_reset_table(pg_cursor, table_name)
        else:
            # Toujours vérifier et réinitialiser la table si elle est vide
            check_and_reset_table(pg_cursor, table_name)

        # Cas spécial pour LIGNE_COMMANDE
        if table_name.upper() == 'LIGNE_COMMANDE':
            # Vérifier si nous avons un ID composite (format: id_commande_id_produit)
            if '_' in row_id:
                parts = row_id.split('_')
                id_commande = parts[0]
                id_produit = parts[1]
                mysql_cursor.execute(f"""
                    SELECT * FROM `{table_name}` 
                    WHERE `id_commande` = %s AND `id_produit` = %s
                """, (id_commande, id_produit))
            else:
                # Si on a seulement l'id_commande (ce qui peut être le cas avec la sélection multiple)
                # Prenons la première ligne correspondante
                mysql_cursor.execute(f"""
                    SELECT * FROM `{table_name}` 
                    WHERE `id_commande` = %s
                    LIMIT 1
                """, (row_id,))
        else:
            # Cas standard pour les autres tables
            mysql_cursor.execute(f"SELECT * FROM `{table_name}` WHERE `{id_column}` = %s", (row_id,))
            
        row_data = mysql_cursor.fetchone()
        if not row_data:
            return jsonify({"error": "Ligne non trouvée"}), 404
        
        # S'assurer que la table de mapping existe
        mapping_table = ensure_mapping_table_exists(pg_cursor, db_name, table_name)

        columns = []
        placeholders = []
        values = []
        
        for col_name, value in row_data.items():
            col_name_lower = col_name.lower()
            
            # Mapper les ID pour les clés étrangères
            if col_name.lower().startswith('id_'):
                new_value = map_foreign_key(pg_cursor, db_name, table_name, col_name, value)
                if new_value is not None:
                    value = new_value

            columns.append(f'"{col_name_lower}"')
            placeholders.append('%s')
            values.append(value)

        if not columns:
            return jsonify({"error": "Aucune colonne valide à insérer"}), 400

        # Insertion dans PostgreSQL
        insert_sql = f"""
            INSERT INTO {table_name.lower()} ({', '.join(columns)})
            VALUES ({', '.join(placeholders)})
            RETURNING *
        """
        
        pg_cursor.execute(insert_sql, values)
        new_row = pg_cursor.fetchone()
        
        # Gestion de l'enregistrement du mapping selon le type de table
        if table_name.upper() == 'LIGNE_COMMANDE':
            # Pour LIGNE_COMMANDE, créer un ID composite pour le mapping
            old_composite_id = f"{row_data['id_commande']}_{row_data['id_produit']}"
            
            # Trouver les indices des colonnes id_commande et id_produit dans le résultat
            try:
                pg_cursor.execute(f"SELECT column_name FROM information_schema.columns WHERE table_name = '{table_name.lower()}'")
                pg_columns = [col[0] for col in pg_cursor.fetchall()]
                id_commande_idx = pg_columns.index('id_commande')
                id_produit_idx = pg_columns.index('id_produit')
                new_composite_id = f"{new_row[id_commande_idx]}_{new_row[id_produit_idx]}"
            except (ValueError, IndexError):
                # Fallback si on ne peut pas déterminer les indices
                new_composite_id = f"{new_row[0]}_{new_row[1]}"
            
            pg_cursor.execute(f"""
                INSERT INTO {mapping_table} (table_name, old_id, new_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (table_name, old_id) DO UPDATE SET new_id = EXCLUDED.new_id
            """, (table_name.lower(), old_composite_id, new_composite_id))
        else:
            # Cas standard pour les tables à clé primaire simple
            pg_cursor.execute(f"""
                INSERT INTO {mapping_table} (table_name, old_id, new_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (table_name, old_id) DO UPDATE SET new_id = EXCLUDED.new_id
            """, (table_name.lower(), str(row_id), str(new_row[0])))

        pg_conn.commit()

        return jsonify({"success": True, "message": "Données transférées avec succès"})

    except Exception as e:
        if pg_conn:
            try:
                pg_conn.rollback()
            except:
                pass
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if mysql_cursor: mysql_cursor.close()
        if mysql_conn: mysql_conn.close()
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()

@app.route('/update-in-postgres', methods=['POST'])
def update_in_postgres():
    mysql_conn = None
    pg_conn = None
    mysql_cursor = None
    pg_cursor = None

    try:
        db_name = request.form.get('db_name')
        table_name = request.form.get('table_name')
        row_id = request.form.get('row_id')
        id_column = request.form.get('id_column')

        # Connexions
        mysql_conn = get_mysql_connection()
        mysql_conn.database = db_name
        mysql_cursor = mysql_conn.cursor(dictionary=True)

        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()
        
        # Récupérer les données actuelles de MySQL
        if table_name.upper() == 'LIGNE_COMMANDE' and '_' in row_id:
            parts = row_id.split('_')
            id_commande = parts[0]
            id_produit = parts[1]
            mysql_cursor.execute(f"""
                SELECT * FROM `{table_name}` 
                WHERE `id_commande` = %s AND `id_produit` = %s
            """, (id_commande, id_produit))
        else:
            mysql_cursor.execute(f"SELECT * FROM `{table_name}` WHERE `{id_column}` = %s", (row_id,))
            
        row_data = mysql_cursor.fetchone()
        if not row_data:
            return jsonify({"error": "Ligne non trouvée dans MySQL"}), 404
            
        # Récupérer le mapping pour trouver l'ID dans PostgreSQL
        mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
        
        if table_name.upper() == 'LIGNE_COMMANDE' and '_' in row_id:
            # Pour LIGNE_COMMANDE, chercher avec l'ID composite
            pg_cursor.execute(f"""
                SELECT new_id FROM {mapping_table}
                WHERE old_id = %s
            """, (row_id,))
        else:
            # Cas standard
            pg_cursor.execute(f"""
                SELECT new_id FROM {mapping_table}
                WHERE old_id = %s
            """, (str(row_id),))
            
        mapping_result = pg_cursor.fetchone()
        if not mapping_result:
            return jsonify({"error": "Mapping non trouvé, cette ligne n'a pas été transférée auparavant."}), 404
            
        new_id = mapping_result[0]
            
        # Préparer les données pour la mise à jour
        columns = []
        update_values = []
        
        for col_name, value in row_data.items():
            col_name_lower = col_name.lower()
            
            # Mapper les ID pour les clés étrangères
            if col_name.lower().startswith('id_'):
                new_value = map_foreign_key(pg_cursor, db_name, table_name, col_name, value)
                if new_value is not None:
                    value = new_value
            
            # Ne pas mettre à jour les clés primaires
            if col_name != id_column:
                columns.append(f'"{col_name_lower}" = %s')
                update_values.append(value)
        
        if not columns:
            return jsonify({"error": "Aucune colonne à mettre à jour"}), 400
            
        # Construire la requête UPDATE
        if table_name.upper() == 'LIGNE_COMMANDE' and '_' in new_id:
            # Pour LIGNE_COMMANDE avec clé composite
            new_id_parts = new_id.split('_')
            update_sql = f"""
                UPDATE {table_name.lower()}
                SET {', '.join(columns)}
                WHERE id_commande = %s AND id_produit = %s
            """
            update_values.extend(new_id_parts)
        else:
            # Pour les tables avec clé simple
            primary_keys = get_primary_keys(mysql_cursor, table_name)
            pk_column = primary_keys[0].lower()
            
            update_sql = f"""
                UPDATE {table_name.lower()}
                SET {', '.join(columns)}
                WHERE "{pk_column}" = %s
            """
            update_values.append(new_id)
            
        # Exécuter la mise à jour
        pg_cursor.execute(update_sql, update_values)
        pg_conn.commit()
        
        return jsonify({"success": True, "message": "Données mises à jour avec succès"})
            
    except Exception as e:
        if pg_conn:
            try:
                pg_conn.rollback()
            except:
                pass
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
        
    finally:
        if mysql_cursor: mysql_cursor.close()
        if mysql_conn: mysql_conn.close()
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()
# Nouvelle route pour modifier une ligne déjà transférée
@app.route('/edit/<db_name>/<table_name>/<row_id>', methods=['GET', 'POST'])
def edit_row(db_name, table_name, row_id):
    if request.method == 'GET':
        mysql_conn = None
        mysql_cursor = None
        pg_conn = None
        pg_cursor = None
        
        try:
            # Connexion à MySQL pour obtenir les données originales
            mysql_conn = get_mysql_connection()
            mysql_conn.database = db_name
            mysql_cursor = mysql_conn.cursor(dictionary=True)
            
            # Récupérer les clés primaires
            primary_keys = get_primary_keys(mysql_cursor, table_name)
            
            # Récupérer les données originales
            if table_name.upper() == 'LIGNE_COMMANDE' and '_' in row_id:
                parts = row_id.split('_')
                id_commande = parts[0]
                id_produit = parts[1]
                mysql_cursor.execute(f"""
                    SELECT * FROM `{table_name}` 
                    WHERE `id_commande` = %s AND `id_produit` = %s
                """, (id_commande, id_produit))
            else:
                mysql_cursor.execute(f"SELECT * FROM `{table_name}` WHERE `{primary_keys[0]}` = %s", (row_id,))
            
            row_data = mysql_cursor.fetchone()
            if not row_data:
                return "Ligne non trouvée", 404
            
            # Obtenir la structure de la table
            mysql_cursor.execute(f"SHOW COLUMNS FROM `{table_name}`")
            columns_info = mysql_cursor.fetchall()
            
            return render_template('edit_row.html',
                                  db_name=db_name,
                                  table_name=table_name,
                                  row_id=row_id,
                                  row_data=row_data,
                                  columns_info=columns_info,
                                  primary_keys=primary_keys)
            
        except Exception as e:
            traceback.print_exc()
            return f"Erreur: {str(e)}", 500
            
        finally:
            if mysql_cursor: mysql_cursor.close()
            if mysql_conn: mysql_conn.close()
            if pg_cursor: pg_cursor.close()
            if pg_conn: pg_conn.close()
    
    elif request.method == 'POST':
        mysql_conn = None
        pg_conn = None
        mysql_cursor = None
        pg_cursor = None
        
        try:
            # Récupérer les données du formulaire
            form_data = request.form.to_dict()
            
            # Connexions
            mysql_conn = get_mysql_connection()
            mysql_conn.database = db_name
            mysql_cursor = mysql_conn.cursor(dictionary=True)
            
            pg_conn = get_postgres_connection()
            pg_cursor = pg_conn.cursor()
            
            # Récupérer les clés primaires
            primary_keys = get_primary_keys(mysql_cursor, table_name)
            
            # Récupérer le mapping de l'ID
            mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
            
            if table_name.upper() == 'LIGNE_COMMANDE' and '_' in row_id:
                # Cas spécial pour LIGNE_COMMANDE avec clé composite
                pg_cursor.execute(f"""
                    SELECT new_id FROM {mapping_table}
                    WHERE old_id = %s
                """, (row_id,))
                
                # Mise à jour dans MySQL également pour LIGNE_COMMANDE
                parts = row_id.split('_')
                id_commande = parts[0]
                id_produit = parts[1]
                
                # Préparer la mise à jour MySQL pour LIGNE_COMMANDE
                mysql_set_clauses = []
                mysql_update_values = []
                
                for key, value in form_data.items():
                    if key not in primary_keys and key != 'csrf_token':
                        mysql_set_clauses.append(f"`{key}` = %s")
                        mysql_update_values.append(value)
                
                if mysql_set_clauses:
                    mysql_update_sql = f"""
                        UPDATE `{table_name}` 
                        SET {', '.join(mysql_set_clauses)} 
                        WHERE `id_commande` = %s AND `id_produit` = %s
                    """
                    mysql_update_values.extend([id_commande, id_produit])
                    mysql_cursor.execute(mysql_update_sql, mysql_update_values)
                    mysql_conn.commit()
            else:
                # Cas standard
                pg_cursor.execute(f"""
                    SELECT new_id FROM {mapping_table}
                    WHERE old_id = %s
                """, (row_id,))
                
                # Mise à jour dans MySQL également
                mysql_set_clauses = []
                mysql_update_values = []
                
                for key, value in form_data.items():
                    if key not in primary_keys and key != 'csrf_token':
                        mysql_set_clauses.append(f"`{key}` = %s")
                        mysql_update_values.append(value)
                
                if mysql_set_clauses:
                    mysql_update_sql = f"""
                        UPDATE `{table_name}` 
                        SET {', '.join(mysql_set_clauses)} 
                        WHERE `{primary_keys[0]}` = %s
                    """
                    mysql_update_values.append(row_id)
                    mysql_cursor.execute(mysql_update_sql, mysql_update_values)
                    mysql_conn.commit()
                
            mapping_result = pg_cursor.fetchone()
            if not mapping_result:
                return "Mapping non trouvé", 404
                
            new_id = mapping_result[0]
            
            # Préparer la requête de mise à jour pour PostgreSQL
            set_clauses = []
            update_values = []
            
            # Exclure les clés primaires de la mise à jour
            for key, value in form_data.items():
                if key not in primary_keys and key != 'csrf_token':
                    # Mapper les clés étrangères si nécessaire
                    if key.startswith('id_') and key != 'id_column':
                        mapped_value = map_foreign_key(pg_cursor, db_name, table_name, key, value)
                        if mapped_value is not None:
                            value = mapped_value
                    
                    set_clauses.append(f'"{key.lower()}" = %s')
                    update_values.append(value)
            
            if not set_clauses:
                return "Aucune donnée à mettre à jour", 400
                
            # Construire la condition WHERE selon le type de table
            if table_name.upper() == 'LIGNE_COMMANDE' and '_' in new_id:
                # Pour LIGNE_COMMANDE avec clé composite
                new_id_parts = new_id.split('_')
                where_clause = f'"id_commande" = %s AND "id_produit" = %s'
                update_values.extend(new_id_parts)
            else:
                # Pour les tables avec clé simple
                pk_column = primary_keys[0].lower()
                where_clause = f'"{pk_column}" = %s'
                update_values.append(new_id)
                
            # Exécuter la mise à jour dans PostgreSQL
            update_sql = f"""
                UPDATE {table_name.lower()}
                SET {', '.join(set_clauses)}
                WHERE {where_clause}
            """
            
            pg_cursor.execute(update_sql, update_values)
            pg_conn.commit()
            
            # Rediriger vers la page de la table
            return redirect(url_for('show_table', db_name=db_name, table_name=table_name))
            
        except Exception as e:
            if pg_conn:
                try:
                    pg_conn.rollback()
                except:
                    pass
            if mysql_conn:
                try:
                    mysql_conn.rollback()
                except:
                    pass
            traceback.print_exc()
            return f"Erreur: {str(e)}", 500
            
        finally:
            if mysql_cursor: mysql_cursor.close()
            if mysql_conn: mysql_conn.close()
            if pg_cursor: pg_cursor.close()
            if pg_conn: pg_conn.close()


# Nouvelle route pour reprendre à la dernière page visitée
@app.route('/resume')
def resume_session():
    db_name = session.get('current_db')
    table_name = session.get('current_table')
    
    if db_name and table_name:
        return redirect(url_for('show_table', db_name=db_name, table_name=table_name))
    elif db_name:
        return redirect(url_for('show_database', db_name=db_name))
    else:
        return redirect(url_for('index'))
@app.route('/delete-from-postgres', methods=['POST'])
def delete_from_postgres():
    pg_conn = None
    pg_cursor = None
    try:
        db_name = request.form.get('db_name')
        table_name = request.form.get('table_name')
        row_id = request.form.get('row_id')
        id_column = request.form.get('id_column')

        # Connexion à PostgreSQL
        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()

        # Récupérer le mapping pour trouver l'ID dans PostgreSQL
        mapping_table = f"id_mapping_{db_name.lower()}_{table_name.lower()}"
        pg_cursor.execute(f"""
            SELECT new_id FROM {mapping_table}
            WHERE old_id = %s
        """, (str(row_id),))
        mapping_result = pg_cursor.fetchone()
        if not mapping_result:
            return jsonify({"error": "Mapping non trouvé, cette ligne n'a pas été transférée auparavant."}), 404
        new_id = mapping_result[0]

        # Construire et exécuter la requête de suppression
        if table_name.upper() == 'LIGNE_COMMANDE' and '_' in new_id:
            # Pour LIGNE_COMMANDE avec clé composite
            new_id_parts = new_id.split('_')
            delete_sql = f"""
                DELETE FROM {table_name.lower()}
                WHERE id_commande = %s AND id_produit = %s
            """
            pg_cursor.execute(delete_sql, new_id_parts)
        else:
            # Pour les tables avec clé simple
            primary_keys = get_primary_keys(pg_cursor, table_name)
            pk_column = primary_keys[0].lower()
            delete_sql = f"""
                DELETE FROM {table_name.lower()}
                WHERE "{pk_column}" = %s
            """
            pg_cursor.execute(delete_sql, (new_id,))

        pg_conn.commit()
        return jsonify({"success": True, "message": "Ligne supprimée avec succès.", "deleted_row_id": row_id})  # Renvoyer l'ID de la ligne supprimée

    except Exception as e:
        if pg_conn:
            try:
                pg_conn.rollback()
            except:
                pass
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    finally:
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()
# Nouvelle route pour voir l'état des transferts
@app.route('/transfer-status')
def transfer_status():
    pg_conn = None
    pg_cursor = None

    try:
        pg_conn = get_postgres_connection()
        pg_cursor = pg_conn.cursor()
        
        # Récupérer la liste des tables de mapping
        pg_cursor.execute("""
            SELECT table_name 
            FROM information_schema.tables 
            WHERE table_name LIKE 'id_mapping_%'
        """)
        
        mapping_tables = [table[0] for table in pg_cursor.fetchall()]
        
        # Organiser les données par base de données et table
        status = {}
        
        for table in mapping_tables:
            # Extraire le db_name et table_name du nom de la table de mapping
            parts = table.split('_', 2)  # id_mapping_db_table
            if len(parts) >= 3:
                db_name = parts[2].split('_')[0]
                table_name = '_'.join(parts[2].split('_')[1:])
                
                if db_name not in status:
                    status[db_name] = {}
                
                # Compter les entrées dans la table de mapping
                pg_cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = pg_cursor.fetchone()[0]
                
                status[db_name][table_name] = count
        
        return render_template('transfer_status.html', status=status)
        
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
        
    finally:
        if pg_cursor: pg_cursor.close()
        if pg_conn: pg_conn.close()

if __name__ == '__main__':
    app.run(debug=True)