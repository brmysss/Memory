#!/bin/bash
set -e

echo "初始化数据目录..."

# 确保数据目录存在并有正确权限
mkdir -p /app/data
chmod 755 /app/data

# 检查数据库文件是否存在
if [ ! -f "/app/data/db.sqlite3" ]; then
    echo "数据库文件不存在，将在应用启动时自动创建"
    DB_IS_NEW=true
else
    echo "数据库文件已存在"
    chmod 644 /app/data/db.sqlite3
    DB_IS_NEW=false
fi

# 简单的数据库备份
create_database_backup() {
    if [ -f "/app/data/db.sqlite3" ] && [ -s "/app/data/db.sqlite3" ]; then
        BACKUP_FILE="/app/data/db_backup_$(date +%Y%m%d_%H%M%S).sqlite3"
        cp "/app/data/db.sqlite3" "$BACKUP_FILE"
        
        # 只保留最近3个备份文件
        ls -t /app/data/db_backup_*.sqlite3 2>/dev/null | tail -n +4 | xargs -r rm
    fi
}

# 检查是否需要运行迁移
if [ "$DB_IS_NEW" = "false" ]; then
    create_database_backup
else
    echo "初始化新数据库"
    python -m migrations.migrations_001
    
    if [ -f "migrations/migrations_003_storage_defaults.py" ]; then
        python -m migrations.migrations_003_storage_defaults
    fi
    
    create_database_backup
fi

echo "启动应用..."

# 启动应用
exec "$@"
