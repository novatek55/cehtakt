import os

# На проде БД = MySQL/MariaDB через PyMySQL (чистый Python, без компиляции).
# Шим версии — чтобы пройти проверку версии драйвера в Django 6.
if os.environ.get('DB_ENGINE') == 'mysql':
    import pymysql

    # Django 6 требует драйвер >= 2.2.1. Проверка версии — на version_info драйвера;
    # PyMySQL API-совместим, фичи Django гейтит по версии сервера (в рантайме), не драйвера.
    pymysql.version_info = (2, 2, 7, 'final', 0)
    pymysql.install_as_MySQLdb()
