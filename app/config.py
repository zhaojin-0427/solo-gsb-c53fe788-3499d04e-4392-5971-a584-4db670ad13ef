"""全局配置（均可通过环境变量覆盖）。"""
import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


DATA_DIR = _env("WH_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data"))
DB_PATH = os.environ.get("WH_DB_PATH", os.path.join(DATA_DIR, "webhook_lab.db"))

# 调度器每隔多少真实毫秒扫描一次到期任务
TICK_MS = int(_env("WH_TICK_MS", "200"))
# 新演练默认的虚拟时钟倍率（0 = 瞬间模式）
DEFAULT_SPEED = int(_env("WH_DEFAULT_SPEED", "10"))
# SSE 时钟广播间隔（真实毫秒）
CLOCK_PUSH_MS = int(_env("WH_CLOCK_PUSH_MS", "1000"))

# 执行器投递的真实 HTTP 超时（秒）。接收端“超时”规则会挂起连接直到此时长。
REQUEST_TIMEOUT_SEC = float(_env("WH_REQUEST_TIMEOUT_SEC", "2.5"))
# 内置 mock 接收端挂起上限（秒）
RECEIVER_HANG_SEC = float(_env("WH_RECEIVER_HANG_SEC", "30"))

# 内置 mock 接收端挂载前缀
RECEIVER_PREFIX = "/receiver"
# 执行器向 mock 接收端发请求使用的基地址（容器内走回环；可改为独立接收端地址）
RECEIVER_BASE_URL = _env("WH_RECEIVER_BASE_URL", "http://127.0.0.1:8000")

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
