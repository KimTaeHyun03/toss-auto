"""실시간 모니터링 대시보드 (읽기 전용).

trader 와 별개 프로세스로 떠서 토스 계좌 상태 + 로컬 상태/주문로그를 보여준다.
주문을 내지 않으며(읽기 전용), trader 의 단일 인스턴스 락과도 무관하다.

실행:  python dashboard.py   →  http://127.0.0.1:8787
토스 API rate limit 보호를 위해 서버측에서 DASHBOARD_CACHE_SEC(기본 5초) 캐시한다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify

from config import Config
from toss_client import TossClient, TossError

try:  # 장 상태는 있으면 표시, 없어도 대시보드는 동작
    from market import market_status, resolve_allowed
except Exception:  # pragma: no cover
    market_status = None  # type: ignore

KST = ZoneInfo("Asia/Seoul")
STATE_DIR = Path(__file__).parent / "state"
ORDERS_CSV = STATE_DIR / "orders.csv"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("dashboard")

app = Flask(__name__)
cfg = Config()
cfg.validate()
toss = TossClient(cfg.toss_client_id, cfg.toss_client_secret, cfg.account_seq)

_CACHE_TTL = float(os.getenv("DASHBOARD_CACHE_SEC", "5"))
_cache: dict = {"at": 0.0, "data": None}
_acct_ready = False
_names: dict[str, str] = {}  # symbol → 한국어 종목명 (정적이라 누적 캐시)


def _resolve_names(symbols) -> dict[str, str]:
    """심볼 → 한국어 종목명. 미캐시 심볼만 stocks() 로 한 번에 조회해 누적한다."""
    uniq = {s for s in symbols if s}
    missing = [s for s in uniq if s not in _names]
    if missing:
        try:
            for info in toss.stocks(missing):
                sym = info.get("symbol")
                if sym:
                    _names[sym] = info.get("name") or sym
        except TossError as e:
            log.warning("종목명 조회 실패: %s", e)
    return {s: _names.get(s, s) for s in uniq}


def _ensure_account() -> None:
    """계좌 seq 를 최초 1회만 지연 해소(네트워크). import 가 네트워크에 의존하지 않게 한다."""
    global _acct_ready
    if not _acct_ready:
        toss.resolve_account_seq()
        _acct_ready = True


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _daily_state() -> dict:
    """오늘자 state json (킬스위치/주문수/시작자산). 없으면 빈 상태."""
    p = STATE_DIR / f"state_{datetime.now(KST):%Y%m%d}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"date": datetime.now(KST).strftime("%Y%m%d"), "trades": 0, "killed": False,
            "kill_reason": "", "equity_start": None}


def _recent_orders(limit: int = 25) -> list[dict]:
    """orders.csv 최근 주문(최신순)."""
    if not ORDERS_CSV.exists():
        return []
    try:
        with ORDERS_CSV.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return list(reversed(rows[-limit:]))


def _session_label() -> str:
    if market_status is None:
        return "—"
    try:
        cal = toss.market_calendar_kr()
        open_now, _key, reason = market_status(cal, resolve_allowed(cfg.trade_sessions))
        return ("🟢 " if open_now else "🔴 ") + reason
    except Exception:
        return "—"


def _gather() -> dict:
    """토스 계좌 + 로컬 상태를 한 번에 수집. 토스 호출 실패해도 로컬 정보는 채운다."""
    st = _daily_state()
    out: dict = {
        "now": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "dry_run": cfg.dry_run,
        "limits": {
            "max_trades": cfg.max_trades_per_day,
            "max_order_krw": cfg.max_order_krw,
            "max_daily_loss_krw": cfg.max_daily_loss_krw,
        },
        "state": {
            "trades": st.get("trades", 0),
            "killed": st.get("killed", False),
            "kill_reason": st.get("kill_reason", ""),
            "equity_start": st.get("equity_start"),
        },
        "orders": _recent_orders(),
        "session": _session_label(),
        "error": None,
    }
    all_syms: list[str] = [o.get("symbol") for o in out["orders"] if o.get("symbol")]
    try:
        _ensure_account()
        holdings = toss.holdings()
        bp = toss.buying_power()
        mv = (holdings.get("marketValue") or {}).get("amount") or {}
        market_value = _f(mv.get("krw"))
        equity = market_value + bp
        dp = (holdings.get("dailyProfitLoss") or {}).get("amount") or {}
        daily_pnl = _f(dp.get("krw"))

        items = []
        for it in (holdings.get("items") or []):
            avg = _f(it.get("averagePurchasePrice"))
            last = _f(it.get("lastPrice"))
            qty = _f(it.get("quantity"))
            items.append({
                "symbol": it.get("symbol"),
                "quantity": qty,
                "avg": avg,
                "last": last,
                "pnl_pct": ((last - avg) / avg * 100) if avg else 0.0,
                "eval_krw": last * qty,
            })

        open_ords = [{
            "symbol": o.get("symbol"), "side": o.get("side"), "type": o.get("orderType"),
            "price": o.get("price"), "quantity": o.get("quantity"),
            "status": o.get("status"), "orderedAt": o.get("orderedAt"), "orderId": o.get("orderId"),
        } for o in toss.open_orders()]

        eq_start = st.get("equity_start")
        out["account"] = {
            "equity": equity,
            "market_value": market_value,
            "buying_power": bp,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": (daily_pnl / (equity - daily_pnl) * 100) if (equity - daily_pnl) else 0.0,
            "equity_change": (equity - eq_start) if eq_start else None,  # 킬스위치 기준 손익
            "holdings": items,
            "open_orders": open_ords,
        }
        all_syms += [i["symbol"] for i in items if i["symbol"]]
        all_syms += [o["symbol"] for o in open_ords if o["symbol"]]
    except TossError as e:
        log.warning("토스 조회 실패: %s", e)
        out["error"] = f"토스 API 조회 실패: {e}"
        out["account"] = None
    out["names"] = _resolve_names(all_syms)  # symbol → 한국어 종목명
    return out


@app.route("/api/state")
def api_state():
    now = time.time()
    if _cache["data"] is None or now - _cache["at"] > _CACHE_TTL:
        _cache["data"] = _gather()
        _cache["at"] = now
    return jsonify(_cache["data"])


@app.route("/")
def index():
    return INDEX_HTML


INDEX_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>토스 자동매매 대시보드</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
        --up:#f85149;--down:#3fb950;--accent:#58a6ff;--warn:#d29922}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);
       font-family:-apple-system,"Segoe UI",Roboto,"Malgun Gothic",sans-serif;font-size:14px}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--bd)}
  header h1{font-size:16px;margin:0}
  .pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--bd);color:var(--mut)}
  .pill.live{color:#fff;background:#1f6feb;border-color:#1f6feb}
  .pill.dry{color:#000;background:var(--warn);border-color:var(--warn)}
  .pill.kill{color:#fff;background:var(--up);border-color:var(--up)}
  main{padding:20px;max-width:1100px;margin:0 auto;display:grid;gap:16px}
  .row{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}
  .card .k{color:var(--mut);font-size:12px;margin-bottom:6px}
  .card .v{font-size:20px;font-weight:600;font-variant-numeric:tabular-nums}
  .up{color:var(--up)} .down{color:var(--down)} .mut{color:var(--mut)}
  section h2{font-size:13px;color:var(--mut);font-weight:600;margin:4px 0 8px;
             text-transform:uppercase;letter-spacing:.04em}
  table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
  th,td{text-align:right;padding:7px 10px;border-bottom:1px solid var(--bd);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--mut);font-weight:500;font-size:12px}
  .empty{color:var(--mut);padding:14px;text-align:center}
  .err{background:#3d1418;border:1px solid var(--up);color:#ffb3b0;padding:10px 14px;border-radius:8px}
  .side-BUY{color:var(--up)} .side-SELL{color:var(--down)}
  footer{color:var(--mut);font-size:12px;text-align:center;padding:0 0 24px}
</style></head>
<body>
<header>
  <h1>토스 자동매매 대시보드</h1>
  <span id="mode" class="pill">—</span>
  <span id="kill" class="pill" style="display:none">🛑 킬스위치</span>
  <span id="session" class="pill">—</span>
  <span class="pill" style="margin-left:auto" id="updated">연결 중…</span>
</header>
<main>
  <div id="err" class="err" style="display:none"></div>
  <div class="row">
    <div class="card"><div class="k">계좌 자산(현금+평가)</div><div class="v" id="equity">—</div></div>
    <div class="card"><div class="k">당일 손익</div><div class="v" id="pnl">—</div></div>
    <div class="card"><div class="k">매수 가능</div><div class="v" id="bp">—</div></div>
    <div class="card"><div class="k">시작 대비(킬스위치 기준)</div><div class="v" id="eqchg">—</div></div>
    <div class="card"><div class="k">오늘 주문</div><div class="v" id="trades">—</div></div>
  </div>
  <section><h2>보유 종목</h2>
    <div class="card" style="padding:0">
      <table><thead><tr><th>종목</th><th>수량</th><th>평단</th><th>현재가</th>
        <th>평가손익%</th><th>평가금액</th></tr></thead>
        <tbody id="holdings"><tr><td colspan="6" class="empty">—</td></tr></tbody></table>
    </div></section>
  <section><h2>미체결 주문</h2>
    <div class="card" style="padding:0">
      <table><thead><tr><th>종목</th><th>구분</th><th>유형</th><th>지정가</th>
        <th>수량</th><th>상태</th></tr></thead>
        <tbody id="open"><tr><td colspan="6" class="empty">—</td></tr></tbody></table>
    </div></section>
  <section><h2>최근 주문 (orders.csv)</h2>
    <div class="card" style="padding:0">
      <table><thead><tr><th>시각</th><th>종목</th><th>구분</th><th>수량</th><th>가격</th>
        <th>예상금액</th><th>conf</th><th>모드</th><th>사유</th></tr></thead>
        <tbody id="orders"><tr><td colspan="9" class="empty">—</td></tr></tbody></table>
    </div></section>
</main>
<footer>자동 새로고침 <span id="every"></span>초 · 읽기 전용 · 주문을 내지 않습니다</footer>
<script>
const REFRESH = 10;
document.getElementById('every').textContent = REFRESH;
const won = n => (n==null||isNaN(n)) ? '—' : Math.round(n).toLocaleString('ko-KR')+'원';
const sgn = n => (n>0?'up':(n<0?'down':'mut'));
const pct = n => (n==null||isNaN(n)) ? '—' : (n>0?'+':'')+n.toFixed(2)+'%';
let NAMES = {};
const esc = s => (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
// 한국어 종목명 + 코드(작게). 이름이 없으면 코드만.
const nm = sym => {const n=NAMES[sym]; return n && n!==sym
  ? `${esc(n)}<div class="mut" style="font-size:11px">${esc(sym)}</div>` : esc(sym);};

function row(html){const tr=document.createElement('tr');tr.innerHTML=html;return tr;}
function fill(id, rows, colspan, empty){
  const tb=document.getElementById(id); tb.innerHTML='';
  if(!rows.length){tb.appendChild(row(`<td colspan="${colspan}" class="empty">${empty}</td>`));return;}
  rows.forEach(r=>tb.appendChild(row(r)));
}

async function tick(){
  let d;
  try{ d = await (await fetch('/api/state')).json(); }
  catch(e){ document.getElementById('updated').textContent='연결 실패'; return; }

  NAMES = d.names || {};
  document.getElementById('updated').textContent = '업데이트 '+d.now;
  document.getElementById('session').textContent = d.session || '—';

  const mode=document.getElementById('mode');
  mode.textContent = d.dry_run ? 'DRY_RUN(모의)' : 'LIVE(실거래)';
  mode.className = 'pill '+(d.dry_run?'dry':'live');

  const kill=document.getElementById('kill');
  if(d.state.killed){kill.style.display='';kill.className='pill kill';kill.title=d.state.kill_reason;}
  else kill.style.display='none';

  const errEl=document.getElementById('err');
  if(d.error){errEl.style.display='';errEl.textContent='⚠ '+d.error;} else errEl.style.display='none';

  document.getElementById('trades').innerHTML =
    `${d.state.trades} <span class="mut" style="font-size:13px">/ ${d.limits.max_trades}</span>`;

  const a=d.account;
  if(a){
    document.getElementById('equity').textContent = won(a.equity);
    const p=document.getElementById('pnl');
    p.innerHTML = `<span class="${sgn(a.daily_pnl)}">${won(a.daily_pnl)} (${pct(a.daily_pnl_pct)})</span>`;
    document.getElementById('bp').textContent = won(a.buying_power);
    const ec=document.getElementById('eqchg');
    ec.innerHTML = a.equity_change==null ? '<span class="mut">집계 전</span>'
      : `<span class="${sgn(a.equity_change)}">${won(a.equity_change)}</span>`;

    fill('holdings', a.holdings.map(h=>
      `<td>${nm(h.symbol)}</td><td>${h.quantity}</td><td>${won(h.avg)}</td><td>${won(h.last)}</td>`+
      `<td class="${sgn(h.pnl_pct)}">${pct(h.pnl_pct)}</td><td>${won(h.eval_krw)}</td>`),
      6, '보유 종목 없음');

    fill('open', a.open_orders.map(o=>
      `<td>${nm(o.symbol)}</td><td class="side-${o.side}">${o.side}</td><td>${o.type}</td>`+
      `<td>${o.price?won(+o.price):'—'}</td><td>${o.quantity}</td><td class="mut">${o.status}</td>`),
      6, '미체결 주문 없음');
  }else{
    ['equity','pnl','bp','eqchg'].forEach(id=>document.getElementById(id).textContent='—');
    fill('holdings',[],6,'계좌 조회 불가'); fill('open',[],6,'계좌 조회 불가');
  }

  fill('orders', d.orders.map(o=>{
    const t=(o.ts_kst||'').replace('T',' ').slice(5,19);
    return `<td>${t}</td><td>${nm(o.symbol)}</td><td class="side-${o.side}">${o.side}</td>`+
      `<td>${o.qty}</td><td>${won(+o.price)}</td><td>${won(+o.est_krw)}</td>`+
      `<td>${o.confidence||'—'}</td><td class="mut">${o.dry_run==='True'?'모의':'실거래'}</td>`+
      `<td style="text-align:left;max-width:280px;overflow:hidden;text-overflow:ellipsis">${o.reason||''}</td>`;
  }), 9, '기록된 주문 없음');
}
tick(); setInterval(tick, REFRESH*1000);
</script>
</body></html>"""


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    log.info("대시보드 시작: http://%s:%d  (DRY_RUN=%s, 캐시=%ss)", host, port, cfg.dry_run, _CACHE_TTL)
    app.run(host=host, port=port, debug=False)
