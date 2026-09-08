
#!/usr/bin/env python3

import os, sys, getpass, pymysql, bcrypt

from dotenv import load_dotenv

load_dotenv('/var/www/admin-portal/.env')



def get_db():

    return pymysql.connect(host='localhost', user=os.getenv('ADMIN_DB_USER', 'adminportal'),

        password=os.getenv('ADMIN_DB_PASSWORD'), database=os.getenv('ADMIN_DB_NAME', 'admin_portal'),

        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)



def create_user():

    print("\n=== Create Admin Portal User ===\n")

    username = input("Username: ").strip()

    email = input("Email: ").strip()

    name = input("Full Name: ").strip()

    print("\nRoles: admin, sales, marketing, support, operations")

    role = input("Role [admin]: ").strip() or 'admin'

    password = getpass.getpass("Password: ")

    if len(password) < 8:

        print("Error: Password must be at least 8 characters")

        return

    password_confirm = getpass.getpass("Confirm Password: ")

    if password != password_confirm:

        print("Error: Passwords do not match")

        return

    password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("INSERT INTO users (username, password_hash, email, name, role) VALUES (%s, %s, %s, %s, %s)",

                (username, password_hash, email, name, role))

        conn.commit()

        conn.close()

        print(f"\n✅ User '{username}' created successfully!")

    except pymysql.err.IntegrityError:

        print(f"\nError: Username '{username}' already exists")

    except Exception as e:

        print(f"\nError: {e}")



def list_users():

    print("\n=== Admin Portal Users ===\n")

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT id, username, name, role, active, last_login FROM users")

            users = cursor.fetchall()

        conn.close()

        for u in users:

            print(f"{u['id']}: {u['username']} ({u['name']}) - {u['role']}")

    except Exception as e:

        print(f"Error: {e}")



if len(sys.argv) < 2:

    print("Usage: python3 create_user.py [create|list]")

elif sys.argv[1] == 'create':

    create_user()

elif sys.argv[1] == 'list':

    list_users()

