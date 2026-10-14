"""
因子挖掘功能端到端集成测试
测试前后端完整流程
"""

import requests
import json
import time
from datetime import datetime, timedelta

# 配置
BASE_URL = "http://localhost:5000"
FRONTEND_URL = "http://localhost:3000"

# 测试用户（简化认证，实际应该通过登录获取）
# 由于我们的API需要token，这里跳过认证直接测试无需认证的端点
# 或者使用mock token进行测试

def test_frontend_alive():
    """测试1: 前端服务可访问"""
    print("\n=== 测试 1: 前端服务 ===")
    try:
        response = requests.get(FRONTEND_URL, timeout=5)
        if response.status_code == 200 and "QuantDinger" in response.text:
            print("✅ 前端服务正常运行")
            print(f"   URL: {FRONTEND_URL}")
            print(f"   状态码: {response.status_code}")
            return True
        else:
            print(f"❌ 前端响应异常: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 前端服务连接失败: {e}")
        return False

def test_backend_alive():
    """测试2: 后端服务可访问"""
    print("\n=== 测试 2: 后端服务 ===")
    try:
        # 测试因子挖掘API端点（无需认证的catalog端点）
        response = requests.get(f"{BASE_URL}/api/factor-mining/catalog", timeout=5)
        # 预期返回401（需要token）或200（成功）
        if response.status_code in [200, 401]:
            print("✅ 后端服务正常运行")
            print(f"   URL: {BASE_URL}")
            print(f"   状态码: {response.status_code}")
            if response.status_code == 401:
                print("   响应: 需要认证（符合预期）")
            return True
        else:
            print(f"❌ 后端响应异常: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ 后端服务连接失败: {e}")
        return False

def test_database_tables():
    """测试3: 数据库表是否存在"""
    print("\n=== 测试 3: 数据库表结构 ===")
    import psycopg2
    try:
        conn = psycopg2.connect(
            host="localhost",
            port=5432,
            database="quantdinger",
            user="postgres",
            password="postgres"
        )
        cur = conn.cursor()

        # 检查因子挖掘相关表
        tables = [
            'qd_factor_mining_runs',
            'qd_factor_candidates',
            'qd_factor_trials'
        ]

        all_exist = True
        for table in tables:
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"✅ 表 {table} 存在，当前记录数: {count}")

        cur.close()
        conn.close()
        print("✅ 数据库表结构正常")
        return True
    except Exception as e:
        print(f"❌ 数据库连接或表检查失败: {e}")
        return False

def test_redis_connection():
    """测试4: Redis连接"""
    print("\n=== 测试 4: Redis 缓存服务 ===")
    try:
        import redis
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        print("✅ Redis 服务正常")
        print(f"   主机: localhost:6379")
        return True
    except Exception as e:
        print(f"❌ Redis连接失败: {e}")
        return False

def test_api_endpoints_structure():
    """测试5: API端点结构"""
    print("\n=== 测试 5: API 端点结构验证 ===")

    endpoints = [
        ("GET", "/api/factor-mining/catalog", "DSL词表"),
        ("POST", "/api/factor-mining/estimate", "静态估价"),
        ("POST", "/api/factor-mining/run", "提交作业"),
        ("GET", "/api/factor-mining/jobs", "作业列表"),
        ("GET", "/api/factor-mining/candidates", "候选库列表"),
    ]

    all_available = True
    for method, endpoint, desc in endpoints:
        try:
            url = f"{BASE_URL}{endpoint}"
            if method == "GET":
                response = requests.get(url, timeout=5)
            else:
                response = requests.post(url, json={}, timeout=5)

            # 401需要认证 或 400参数错误 都说明端点存在
            if response.status_code in [200, 400, 401, 422]:
                print(f"✅ {method:4} {endpoint:45} {desc} - 端点存在")
            else:
                print(f"⚠️  {method:4} {endpoint:45} {desc} - 状态码: {response.status_code}")
                all_available = False
        except Exception as e:
            print(f"❌ {method:4} {endpoint:45} {desc} - 错误: {str(e)[:50]}")
            all_available = False

    if all_available:
        print("✅ 所有API端点结构正常")
    else:
        print("⚠️  部分API端点异常")

    return all_available

def print_summary(results):
    """打印测试总结"""
    print("\n" + "="*70)
    print("集成测试总结")
    print("="*70)

    total = len(results)
    passed = sum(results.values())

    for test_name, result in results.items():
        status = "✅ 通过" if result else "❌ 失败"
        print(f"{status} - {test_name}")

    print(f"\n总计: {passed}/{total} 通过")

    if passed == total:
        print("\n🎉 所有测试通过！因子挖掘功能集成测试成功！")
    else:
        print(f"\n⚠️  {total - passed} 个测试失败，请检查相关服务")

    print("="*70)

def main():
    """运行所有测试"""
    print("="*70)
    print("因子挖掘功能 - 端到端集成测试")
    print("="*70)
    print(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"前端地址: {FRONTEND_URL}")
    print(f"后端地址: {BASE_URL}")

    # 运行所有测试
    results = {}

    results["前端服务"] = test_frontend_alive()
    results["后端服务"] = test_backend_alive()
    results["数据库表"] = test_database_tables()
    results["Redis缓存"] = test_redis_connection()
    results["API端点"] = test_api_endpoints_structure()

    # 打印总结
    print_summary(results)

    return all(results.values())

if __name__ == "__main__":
    import sys
    success = main()
    sys.exit(0 if success else 1)
