import os
from tortoise import Tortoise
from app.utils import logger
import glob
import importlib
import asyncio


async def get_db_connection_config():
    """动态获取数据库连接配置"""
    from app.utils.config import settings
    
    # 首先尝试使用默认SQLite配置连接，以读取数据库设置
    default_config = {
        "db_url": f"sqlite:///{settings.DATA_DIR}/db.sqlite3?timeout=30&journal_mode=WAL&synchronous=NORMAL&cache_size=10000&temp_store=memory",
        "modules": {"models": ["app.models"]},
        "use_tz": False,
        "timezone": "Asia/Shanghai",
    }
    
    try:
        # 临时连接到默认数据库以读取设置
        await Tortoise.init(**default_config)
        
        # 检查setting表是否存在
        connection = Tortoise.get_connection("default")
        try:
            result = await connection.execute_query('SELECT database FROM "setting" WHERE id = 1')
            if result[1] and result[1][0] and result[1][0][0]:
                import json
                db_config = json.loads(result[1][0][0])
                
                # 根据数据库类型构建连接配置
                if db_config.get("database_type") == "postgresql" or db_config.get("database_type") == "neon":
                    ssl_param = "?ssl=true" if db_config.get("neon_ssl", True) else "?ssl=false"
                    db_url = f"postgres://{db_config['neon_username']}:{db_config['neon_password']}@{db_config['neon_host']}:{db_config['neon_port']}/{db_config['neon_database']}{ssl_param}"
                    
                    await Tortoise.close_connections()
                    return {
                        "db_url": db_url,
                        "modules": {"models": ["app.models"]},
                        "use_tz": False,
                        "timezone": "Asia/Shanghai",
                    }
                    
                elif db_config.get("database_type") == "mysql":
                    db_url = f"mysql://{db_config['mysql_username']}:{db_config['mysql_password']}@{db_config['mysql_host']}:{db_config['mysql_port']}/{db_config['mysql_database']}"
                    
                    await Tortoise.close_connections()
                    return {
                        "db_url": db_url,
                        "modules": {"models": ["app.models"]},
                        "use_tz": False,
                        "timezone": "Asia/Shanghai",
                    }
                    
        except Exception as e:
            logger.info(f"无法读取数据库设置，使用默认SQLite配置: {str(e)}")
            
        await Tortoise.close_connections()
        return default_config
        
    except Exception as e:
        logger.info(f"使用默认SQLite配置: {str(e)}")
        return default_config


async def init_db():
    try:
        # 获取动态数据库连接配置
        db_connection = await get_db_connection_config()
        logger.info(f"使用数据库连接: {db_connection['db_url'].split('@')[0]}@***")
        
        await Tortoise.init(**db_connection)
        
        # 验证数据库连接
        connection = Tortoise.get_connection("default")
        await connection.execute_query("SELECT 1")
        logger.info("数据库连接验证成功")

        # 创建migrations表（兼容不同数据库类型）
        db_url = db_connection['db_url']
        if 'postgres' in db_url:
            # PostgreSQL语法
            await connection.execute_script(
                """
                CREATE TABLE IF NOT EXISTS migrates (
                    id SERIAL PRIMARY KEY,
                    migration_file VARCHAR(255) NOT NULL UNIQUE,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
        elif 'mysql' in db_url:
            # MySQL语法
            await connection.execute_script(
                """
                CREATE TABLE IF NOT EXISTS migrates (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    migration_file VARCHAR(255) NOT NULL UNIQUE,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )
        else:
            # SQLite语法
            await connection.execute_script(
                """
                CREATE TABLE IF NOT EXISTS migrates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    migration_file VARCHAR(255) NOT NULL UNIQUE,
                    executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """
            )

        await execute_migrations()
        await ensure_sqlite_api_token_schema(connection, db_connection["db_url"])
    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
        raise


async def ensure_sqlite_api_token_schema(connection, db_url: str | None = None):
    try:
        is_sqlite = False
        if db_url and db_url.startswith("sqlite"):
            is_sqlite = True
        else:
            module_name = getattr(connection.__class__, "__module__", "") or ""
            is_sqlite = module_name.startswith("tortoise.backends.sqlite") or "sqlite" in module_name.lower() or "sqlite" in str(type(connection)).lower()
        if not is_sqlite:
            return

        result = await connection.execute_query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='api_token'"
        )
        if not result[1]:
            await connection.execute_script(
                """
                CREATE TABLE IF NOT EXISTS "api_token" (
                    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                    "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    "name" VARCHAR(100) NOT NULL,
                    "token" VARCHAR(255) NOT NULL UNIQUE,
                    "is_permanent" INT NOT NULL DEFAULT 0,
                    "expires_at" TIMESTAMP,
                    "is_active" INT NOT NULL DEFAULT 1,
                    "last_used" TIMESTAMP,
                    "user_id" INT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
                    "remark" TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_api_token_token ON api_token (token);
                CREATE INDEX IF NOT EXISTS idx_api_token_user_id ON api_token (user_id);
                CREATE INDEX IF NOT EXISTS idx_api_token_is_active ON api_token (is_active);
                CREATE INDEX IF NOT EXISTS idx_api_token_expires_at ON api_token (expires_at);
                """
            )
            await ensure_sqlite_api_token_foreign_key(connection)
            return

        table_info = await connection.execute_query('PRAGMA table_info("api_token")')
        existing_cols = {row[1] for row in (table_info[1] or [])}

        columns_to_add = [
            ('remark', 'TEXT'),
            ('last_used', 'TIMESTAMP'),
            ('expires_at', 'TIMESTAMP'),
            ('is_active', 'INT NOT NULL DEFAULT 1'),
            ('is_permanent', 'INT NOT NULL DEFAULT 0'),
            ('name', 'VARCHAR(100) NOT NULL DEFAULT ""'),
            ('token', 'VARCHAR(255) NOT NULL DEFAULT ""'),
            ('created_at', 'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'),
            ('updated_at', 'TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP'),
            ('user_id', 'INT NOT NULL DEFAULT 1'),
        ]

        for col_name, col_def in columns_to_add:
            if col_name not in existing_cols:
                await connection.execute_script(f'ALTER TABLE "api_token" ADD COLUMN "{col_name}" {col_def};')

        await connection.execute_script(
            """
            CREATE INDEX IF NOT EXISTS idx_api_token_token ON api_token (token);
            CREATE INDEX IF NOT EXISTS idx_api_token_user_id ON api_token (user_id);
            CREATE INDEX IF NOT EXISTS idx_api_token_is_active ON api_token (is_active);
            CREATE INDEX IF NOT EXISTS idx_api_token_expires_at ON api_token (expires_at);
            """
        )
        await ensure_sqlite_api_token_foreign_key(connection)
    except Exception as e:
        logger.error(f"API Token表结构检查失败: {str(e)}")


async def ensure_sqlite_api_token_foreign_key(connection):
    try:
        fk_info = await connection.execute_query('PRAGMA foreign_key_list("api_token")')
        fk_rows = fk_info[1] or []
        referenced_tables = {row[2] for row in fk_rows if len(row) > 2}
        if "user_old" not in referenced_tables:
            return

        table_info = await connection.execute_query('PRAGMA table_info("api_token")')
        old_cols = [row[1] for row in (table_info[1] or [])]

        new_cols = [
            "id",
            "created_at",
            "updated_at",
            "name",
            "token",
            "is_permanent",
            "expires_at",
            "is_active",
            "last_used",
            "user_id",
            "remark",
        ]
        copy_cols = [c for c in new_cols if c in old_cols]
        if not copy_cols:
            return

        cols_sql = ", ".join([f'"{c}"' for c in copy_cols])

        await connection.execute_script("PRAGMA foreign_keys=OFF;")
        await connection.execute_script("BEGIN;")
        await connection.execute_script(
            """
            CREATE TABLE IF NOT EXISTS "api_token_new" (
                "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
                "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                "name" VARCHAR(100) NOT NULL,
                "token" VARCHAR(255) NOT NULL UNIQUE,
                "is_permanent" INT NOT NULL DEFAULT 0,
                "expires_at" TIMESTAMP,
                "is_active" INT NOT NULL DEFAULT 1,
                "last_used" TIMESTAMP,
                "user_id" INT NOT NULL REFERENCES "user" ("id") ON DELETE CASCADE,
                "remark" TEXT
            );
            """
        )
        await connection.execute_script(
            f'INSERT INTO "api_token_new" ({cols_sql}) SELECT {cols_sql} FROM "api_token";'
        )
        await connection.execute_script('DROP TABLE "api_token";')
        await connection.execute_script('ALTER TABLE "api_token_new" RENAME TO "api_token";')
        await connection.execute_script(
            """
            CREATE INDEX IF NOT EXISTS idx_api_token_token ON api_token (token);
            CREATE INDEX IF NOT EXISTS idx_api_token_user_id ON api_token (user_id);
            CREATE INDEX IF NOT EXISTS idx_api_token_is_active ON api_token (is_active);
            CREATE INDEX IF NOT EXISTS idx_api_token_expires_at ON api_token (expires_at);
            """
        )
        await connection.execute_script("COMMIT;")
        await connection.execute_script("PRAGMA foreign_keys=ON;")
    except Exception as e:
        try:
            await connection.execute_script("ROLLBACK;")
        except Exception:
            pass
        try:
            await connection.execute_script("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        logger.error(f"API Token外键修复失败: {str(e)}")


async def execute_migrations():
    """执行数据库迁移"""
    try:
        # 收集迁移文件
        migration_files = []
        for root, dirs, _ in os.walk("."):
            if "migrations" in dirs:
                migration_path = os.path.join(root, "migrations")
                migration_files.extend(
                    glob.glob(os.path.join(migration_path, "migrations_*.py"))
                )

        migration_files.sort()
        
        # 获取数据库连接信息以确定参数占位符类型
        connection = Tortoise.get_connection("default")
        # 通过连接类型判断数据库类型
        is_postgres = 'asyncpg' in str(type(connection))
        is_mysql = 'aiomysql' in str(type(connection))
        
        for migration_file in migration_files:
            file_name = os.path.basename(migration_file)
            
            # 根据数据库类型使用不同的参数占位符
            if is_postgres:
                # PostgreSQL使用$1, $2等
                executed = await connection.execute_query(
                    "SELECT id FROM migrates WHERE migration_file = $1", [file_name]
                )
            else:
                # SQLite和MySQL使用?
                executed = await connection.execute_query(
                    "SELECT id FROM migrates WHERE migration_file = ?", [file_name]
                )

            if not executed[1]:
                logger.info(f"执行迁移: {file_name} for {migration_file}")
                module_path = (
                    migration_file.replace("./", "")
                    .replace("/", ".")
                    .replace(".\\", "")
                    .replace("\\", ".")
                    .replace(".py", "")
                )
                try:
                    migration_module = importlib.import_module(module_path)
                    if hasattr(migration_module, "migrate"):
                        await migration_module.migrate()
                        
                        # 根据数据库类型使用不同的参数占位符
                        if is_postgres:
                            await connection.execute_query(
                                "INSERT INTO migrates (migration_file) VALUES ($1)",
                                [file_name],
                            )
                        else:
                            await connection.execute_query(
                                "INSERT INTO migrates (migration_file) VALUES (?)",
                                [file_name],
                            )
                        logger.info(f"迁移完成: {file_name}")
                except Exception as e:
                    logger.error(f"迁移 {file_name} 执行失败: {str(e)}")
                    raise

    except Exception as e:
        logger.error(f"迁移过程发生错误: {str(e)}")
        raise
