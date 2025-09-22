import mysql.connector

def get_db_connection():
    conn = mysql.connector.connect(
        host="localhost",
        user="root",         # change if needed
        password="aniketraj123",  # change if needed
        database="civic_db"
    )
    return conn
