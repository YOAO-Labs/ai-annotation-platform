#!/bin/bash
set -e
echo "=== YOAO 开发环境初始化 ==="

echo "1. 启动基础服务 (PostgreSQL, Redis, MinIO)..."
docker compose up -d postgres redis minio

echo "2. 等待服务就绪..."
sleep 5

echo "3. 安装前端依赖..."
pnpm install

echo "4. 安装后端依赖..."
cd apps/api
pip install -e .
cd ../..

echo "=== 初始化完成 ==="
echo "前端: pnpm dev:web"
echo "后端: pnpm dev:api"
echo "数据库: localhost:5432"
echo "Redis:  localhost:6379"
echo "MinIO:  localhost:9000 (控制台 :9001)"
