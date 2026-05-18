import json
import logging
import os
import sys
import time
from html import escape
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from web3 import Web3


DEFAULT_ENV_PATH = (
    r"C:\Users\13543\Documents\Codex\2026-05-10"
    r"\openclaw-open-ai-5-5\workspace\skills\narrative-radar\.env"
)

ENV_PATH = Path(os.getenv("ALT_FUN_ENV_PATH", DEFAULT_ENV_PATH))
RPC_HTTP_URL = os.getenv("RPC_HTTP_URL", os.getenv("RPC_URL", "https://rpc.hyperliquid.xyz/evm"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "20"))
MAX_LOG_BLOCK_RANGE = int(os.getenv("MAX_LOG_BLOCK_RANGE", "50"))
BACKFILL_BLOCKS = int(os.getenv("BACKFILL_BLOCKS", "0"))
RECONNECT_DELAY = int(os.getenv("RECONNECT_DELAY", "5"))
STATUS_INTERVAL = int(os.getenv("STATUS_INTERVAL", "60"))

STATE_PATH = Path(os.getenv("STATE_PATH", "alt_fun_monitor_state.json"))
LOG_PATH = Path(os.getenv("LOG_PATH", "alt_fun_monitor.log"))
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "3"))

FACTORY_ADDRESS = Web3.to_checksum_address(
    os.getenv("FACTORY_ADDRESS", "0x65a379FE76C7AdC8037b3522De62B27c0D4e9259")
)

WATCH_UNDERLYING_KEYWORDS = ["ASTEROID", "SPCX"]

FACTORY_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "lt", "type": "address"},
            {"indexed": False, "name": "underlying", "type": "string"},
            {"indexed": False, "name": "leverage", "type": "uint256"},
            {"indexed": False, "name": "isLong", "type": "bool"},
        ],
        "name": "CreateLt",
        "type": "event",
    },
    {
        "inputs": [],
        "name": "lts",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]

CREATE_LT_TOPIC = Web3.keccak(text="CreateLt(address,string,uint256,bool)").hex()
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

known_lts = set()
logger = logging.getLogger("alt_fun_monitor")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)


def setup_logging():
    if logger.handlers:
        return

    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def load_env_file(path: Path):
    if not path.exists():
        logger.warning(".env not found: %s", path)
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        if key and key not in os.environ:
            os.environ[key] = value


def get_telegram_config():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID", "").strip()
        or os.getenv("TG_CHAT_ID", "").strip()
    )
    return token, chat_id


def telegram_enabled():
    token, chat_id = get_telegram_config()
    return bool(token and chat_id)


def send_telegram(message):
    token, chat_id = get_telegram_config()
    if not token or not chat_id:
        logger.warning("Telegram not configured; skip notification")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Telegram send failed: %s", exc)


def load_state():
    if not STATE_PATH.exists():
        return None

    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        known_lts.update(str(addr).lower() for addr in state.get("known_lts", []))
        logger.info(
            "Loaded state: known_lts=%s, last_scanned_block=%s",
            len(known_lts),
            state.get("last_scanned_block"),
        )
        return state
    except Exception as exc:
        logger.warning("State load failed; starting with fresh in-memory state: %s", exc)
        return None


def save_state(last_scanned_block):
    state = {
        "last_scanned_block": int(last_scanned_block),
        "known_lts": sorted(known_lts),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temp_path.replace(STATE_PATH)


def connect_web3():
    logger.info("Connecting HTTP RPC: %s", RPC_HTTP_URL)
    w3 = Web3(Web3.HTTPProvider(RPC_HTTP_URL, request_kwargs={"timeout": HTTP_TIMEOUT}))

    if not w3.is_connected():
        raise RuntimeError("HTTP RPC connection failed")

    chain_id = w3.eth.chain_id
    latest_block = w3.eth.block_number
    logger.info("RPC connected. chain_id=%s, latest_block=%s", chain_id, latest_block)
    return w3


def fetch_existing_lts(factory):
    try:
        current = factory.functions.lts().call()
        return [Web3.to_checksum_address(addr) for addr in current]
    except Exception as exc:
        logger.warning("Initial LT load failed; relying on logs only: %s", exc)
        return []


def normalize_value(value):
    if isinstance(value, bytes):
        return Web3.to_hex(value)
    if isinstance(value, str):
        return value.strip("\x00").strip()
    return value


def call_view(w3, address, signature, output_types):
    selector = Web3.to_hex(Web3.keccak(text=signature)[:4])

    try:
        data = w3.eth.call({"to": address, "data": selector})
        if not data:
            return None
        decoded = w3.codec.decode(output_types, data)
        if not decoded:
            return None
        return normalize_value(decoded[0])
    except Exception:
        return None


def call_first(w3, address, candidates):
    for signature, output_types, label in candidates:
        value = call_view(w3, address, signature, output_types)
        if value is None:
            continue
        if output_types == ["address"] and str(value).lower() == ZERO_ADDRESS.lower():
            continue
        return label, value
    return None, None


def is_address(value):
    return isinstance(value, str) and Web3.is_address(value)


def fetch_token_summary(w3, address):
    if not is_address(address):
        return {}

    checksum = Web3.to_checksum_address(address)
    return {
        "address": checksum,
        "name": call_view(w3, checksum, "name()", ["string"]),
        "symbol": call_view(w3, checksum, "symbol()", ["string"]),
        "decimals": call_view(w3, checksum, "decimals()", ["uint8"]),
    }


def fetch_lt_details(w3, lt_address, event_args):
    details = {
        "lt": Web3.to_checksum_address(lt_address),
        "name": call_view(w3, lt_address, "name()", ["string"]),
        "symbol": call_view(w3, lt_address, "symbol()", ["string"]),
        "decimals": call_view(w3, lt_address, "decimals()", ["uint8"]),
        "total_supply": call_view(w3, lt_address, "totalSupply()", ["uint256"]),
        "underlying": event_args.get("underlying"),
        "leverage": event_args.get("leverage"),
        "is_long": event_args.get("isLong"),
        "contract_fields": {},
        "underlying_token": {},
    }

    probes = {
        "underlying": [
            ("underlying()", ["string"], "underlying() string"),
            ("underlying()", ["address"], "underlying() address"),
            ("underlyingAsset()", ["address"], "underlyingAsset() address"),
            ("asset()", ["address"], "asset() address"),
            ("baseToken()", ["address"], "baseToken() address"),
        ],
        "leverage": [
            ("leverage()", ["uint256"], "leverage()"),
            ("targetLeverage()", ["uint256"], "targetLeverage()"),
        ],
        "is_long": [
            ("isLong()", ["bool"], "isLong()"),
            ("long()", ["bool"], "long()"),
        ],
    }

    for key, candidates in probes.items():
        label, value = call_first(w3, lt_address, candidates)
        if value is None:
            continue

        details["contract_fields"][key] = {"source": label, "value": value}

        if key == "underlying" and not details["underlying"]:
            details["underlying"] = value
        elif key == "leverage" and details["leverage"] is None:
            details["leverage"] = value
        elif key == "is_long" and details["is_long"] is None:
            details["is_long"] = value

    underlying_value = details.get("underlying")
    contract_underlying = details["contract_fields"].get("underlying", {}).get("value")
    if is_address(contract_underlying):
        details["underlying_token"] = fetch_token_summary(w3, contract_underlying)
    elif is_address(underlying_value):
        details["underlying_token"] = fetch_token_summary(w3, underlying_value)

    return details


def format_side(is_long):
    if is_long is True:
        return "LONG"
    if is_long is False:
        return "SHORT"
    return "UNKNOWN"


def format_token_supply(value, decimals):
    if value is None:
        return "UNKNOWN"

    try:
        if decimals is None:
            return str(value)
        scaled = int(value) / (10 ** int(decimals))
        return f"{scaled:,.6f}".rstrip("0").rstrip(".")
    except Exception:
        return str(value)


def html_value(value):
    if value is None or value == "":
        return "UNKNOWN"
    return escape(str(value), quote=False)


def is_watch_underlying(details):
    if not details:
        return False

    underlying = str(details.get("underlying") or "").upper()
    return any(keyword in underlying for keyword in WATCH_UNDERLYING_KEYWORDS)


def should_notify_leverage_target(details):
    return is_watch_underlying(details)


def build_telegram_message(details, block_number, tx_hash_text, now):
    is_watch = is_watch_underlying(details)
    title = "🔥🚀【Watch Underlying New Leverage Target】" if is_watch else "alt.fun new leverage target"
    side = format_side(details.get("is_long"))
    leverage = details.get("leverage", "UNKNOWN")
    total_supply = format_token_supply(details.get("total_supply"), details.get("decimals"))
    underlying_token = details.get("underlying_token") or {}

    lines = [
        f"<b>{title}</b>",
        f"LT: <code>{html_value(details['lt'])}</code>",
        f"Underlying: <b>{html_value(details.get('underlying'))}</b>",
        f"Name/Symbol: <b>{html_value(details.get('name'))} / {html_value(details.get('symbol'))}</b>",
        f"Side/Leverage: <b>{html_value(side)} {html_value(leverage)}x</b>",
        f"Decimals/Supply: <b>{html_value(details.get('decimals'))} / {html_value(total_supply)}</b>",
    ]

    if underlying_token:
        lines.append(
            "Underlying token: "
            f"<b>{html_value(underlying_token.get('symbol'))} / {html_value(underlying_token.get('name'))}</b>"
        )
        lines.append(f"Underlying token addr: <code>{html_value(underlying_token.get('address'))}</code>")

    lines.extend(
        [
            "Priority: <b>WATCH_UNDERLYING</b>" if is_watch else "Priority: <b>NORMAL</b>",
            f"Block: <code>{html_value(block_number)}</code>",
            f"Tx: <code>{html_value(tx_hash_text)}</code>",
            f"Time: {html_value(now)}",
        ]
    )
    return "\n".join(lines)


def log_lt_details(details, block_number, tx_hash_text, now):
    is_watch = is_watch_underlying(details)
    spcx_tag = "【WATCH】" if is_watch else ""
    side = format_side(details.get("is_long"))
    logger.info("New LT detected %s at %s", spcx_tag, now)
    logger.info("  LT: %s", details["lt"])
    logger.info("  Name/Symbol: %s / %s", details.get("name"), details.get("symbol"))
    logger.info("  Underlying: %s", details.get("underlying"))
    logger.info("  Side/leverage: %s %sx", side, details.get("leverage"))
    logger.info("  Decimals: %s", details.get("decimals"))
    logger.info(
        "  Total supply: %s",
        format_token_supply(details.get("total_supply"), details.get("decimals")),
    )
    if details.get("contract_fields"):
        logger.info("  Contract probes: %s", json.dumps(details["contract_fields"], default=str))
    if details.get("underlying_token"):
        logger.info("  Underlying token: %s", json.dumps(details["underlying_token"], default=str))
    logger.info("  Block: %s", block_number)
    logger.info("  Tx: %s", tx_hash_text)


def handle_log(w3, factory, log, last_scanned_block):
    try:
        event = factory.events.CreateLt().process_log(log)
        args = event["args"]
        lt_address = Web3.to_checksum_address(args["lt"])
        lt_key = lt_address.lower()

        if lt_key in known_lts:
            return

        known_lts.add(lt_key)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        block_number = log.get("blockNumber", "UNKNOWN")
        tx_hash = log.get("transactionHash")
        tx_hash_text = tx_hash.hex() if tx_hash else "UNKNOWN"
        details = fetch_lt_details(w3, lt_address, args)

        if not should_notify_leverage_target(details):
            logger.info(
                "Detected leverage target but not in watch keywords; skipping notification. lt=%s, underlying=%s, name=%s, symbol=%s",
                lt_address,
                details.get("underlying"),
                details.get("name"),
                details.get("symbol"),
            )
            save_state(last_scanned_block)
            return

        log_lt_details(details, block_number, tx_hash_text, now)
        send_telegram(build_telegram_message(details, block_number, tx_hash_text, now))
        save_state(last_scanned_block)
    except Exception as exc:
        logger.warning("Event decode failed: %s", exc)


def iter_block_ranges(from_block, to_block):
    start = from_block
    while start <= to_block:
        end = min(start + MAX_LOG_BLOCK_RANGE - 1, to_block)
        yield start, end
        start = end + 1


def poll_logs(w3, factory, from_block, to_block, last_scanned_block):
    for start, end in iter_block_ranges(from_block, to_block):
        logs = w3.eth.get_logs(
            {
                "address": FACTORY_ADDRESS,
                "fromBlock": start,
                "toBlock": end,
                "topics": [CREATE_LT_TOPIC],
            }
        )

        if logs:
            logger.info("Found %s CreateLt log(s), blocks %s-%s", len(logs), start, end)

        for log in logs:
            handle_log(w3, factory, log, last_scanned_block)


def get_start_block(state, latest_block):
    if state and state.get("last_scanned_block") is not None:
        return min(int(state["last_scanned_block"]), latest_block)
    return max(0, latest_block - BACKFILL_BLOCKS)


def run_forever():
    setup_logging()
    load_env_file(ENV_PATH)
    state = load_state()

    w3 = connect_web3()
    factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=FACTORY_ABI)

    existing_lts = fetch_existing_lts(factory)
    if state is None:
        known_lts.update(addr.lower() for addr in existing_lts)
        logger.info("Seeded existing LT count: %s", len(known_lts))
    else:
        logger.info(
            "Factory current LT count: %s; state known LT count: %s",
            len(existing_lts),
            len(known_lts),
        )

    latest_block = w3.eth.block_number
    last_scanned_block = get_start_block(state, latest_block)
    save_state(last_scanned_block)

    logger.info("alt.fun BounceTech monitor started (watch underlying enabled)")
    logger.info("Factory: %s", FACTORY_ADDRESS)
    logger.info("Poll interval: %ss", CHECK_INTERVAL)
    logger.info("Starting after block: %s", last_scanned_block)
    logger.info("Telegram: %s", "enabled" if telegram_enabled() else "disabled")
    logger.info("Log file: %s", LOG_PATH.resolve())
    logger.info("State file: %s", STATE_PATH.resolve())

    last_status_ts = time.monotonic()

    while True:
        try:
            latest_block = w3.eth.block_number
            from_block = last_scanned_block + 1

            if from_block <= latest_block:
                poll_logs(w3, factory, from_block, latest_block, last_scanned_block)
                last_scanned_block = latest_block
                save_state(last_scanned_block)

            now = time.monotonic()
            if now - last_status_ts >= STATUS_INTERVAL:
                logger.info(
                    "Heartbeat: latest_block=%s, last_scanned_block=%s, known_lts=%s",
                    latest_block,
                    last_scanned_block,
                    len(known_lts),
                )
                last_status_ts = now

            time.sleep(CHECK_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            return
        except Exception as exc:
            logger.warning("Poll error: %s. Reconnecting in %ss...", exc, RECONNECT_DELAY)
            time.sleep(RECONNECT_DELAY)
            w3 = connect_web3()
            factory = w3.eth.contract(address=FACTORY_ADDRESS, abi=FACTORY_ABI)


if __name__ == "__main__":
    run_forever()
