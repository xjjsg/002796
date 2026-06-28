import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  CircleDollarSign,
  Clock3,
  Database,
  FileClock,
  Gauge,
  LayoutDashboard,
  Menu,
  Play,
  Radio,
  RefreshCw,
  Search,
  Server,
  Settings,
  ShieldCheck,
  Square,
  TrendingDown,
  TrendingUp,
  WalletCards,
  X,
  Zap,
} from "lucide-react";

const NAV_ITEMS = [
  { id: "monitor", label: "实时监控", icon: LayoutDashboard },
  { id: "backtest", label: "回测分析", icon: BarChart3 },
  { id: "trades", label: "交易记录", icon: FileClock },
  { id: "data", label: "数据管理", icon: Database },
  { id: "settings", label: "系统设置", icon: Settings },
];

const FALLBACK_SNAPSHOT = {
  status: "LOADING",
  symbol: { name: "世嘉科技", code: "002796.SZ" },
  feed: { label: "连接中", lastTick: "", fallback: false },
  quote: { price: 0, changePct: 0, vwap: 0, localVwap: 0 },
  account: {
    equity: 0,
    pnl: 0,
    pnlPct: 0,
    shares: 0,
    cash: 0,
    positionPct: 0,
    targetPct: 0,
    floorPct: 0.4,
    ceilingPct: 1,
    mode: "NEUTRAL",
    dayTradeCount: 0,
    maxDayTrades: 5,
  },
  regime: { name: "UNKNOWN", floorPct: 0.4, ceilingPct: 1, detail: "" },
  decision: {
    action: "HOLD",
    headline: "加载中",
    reason: "正在连接本地服务。",
    detail: "",
    restrictions: [],
    leadingSignal: null,
  },
  signals: [],
  factors: [],
  orderbook: { asks: [], bids: [], imbalance: 0 },
  chart: [],
};

function money(value, digits = 0) {
  const number = Number(value || 0);
  return `¥${number.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

function number(value, digits = 2) {
  return Number(value || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pct(value, digits = 2, signed = true) {
  const numeric = Number(value || 0) * 100;
  return `${signed && numeric > 0 ? "+" : ""}${numeric.toFixed(digits)}%`;
}

function bytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${(size / 1024 ** 3).toFixed(2)} GB`;
}

function timeText(value) {
  if (!value) return "--";
  return String(value).replace("T", " ").slice(-8);
}

function actionLabel(action) {
  if (action === "BUY") return "买入";
  if (action === "SELL") return "卖出";
  return "观望";
}

function statusLabel(status) {
  const labels = {
    IDLE: "待启动",
    LOADING: "连接中",
    STARTING: "启动中",
    RUNNING: "实时运行",
    PAUSE: "非交易时段",
    STOPPING: "停止中",
    STOPPED: "已停止",
  };
  return labels[status] || status;
}

function signalStateLabel(state) {
  const labels = {
    triggered: "可执行",
    pending: "等待执行",
    blocked: "条件未满足",
    confirm: "辅助确认",
    near: "接近触发",
    watching: "持续观察",
  };
  return labels[state] || "持续观察";
}

function Sidebar({ page, setPage, open, setOpen }) {
  return (
    <>
      {open && <button className="sidebar-scrim" aria-label="关闭菜单" onClick={() => setOpen(false)} />}
      <aside className={`sidebar ${open ? "sidebar-open" : ""}`}>
        <div className="brand">
          <div className="brand-mark"><Zap size={18} /></div>
          <div>
            <strong>V6 Workbench</strong>
            <span>策略运行控制台</span>
          </div>
        </div>
        <nav className="nav-list">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                className={`nav-item ${page === item.id ? "active" : ""}`}
                onClick={() => {
                  setPage(item.id);
                  setOpen(false);
                }}
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <div className="sidebar-foot">
          <span className="mode-dot" />
          <div>
            <strong>模拟账户</strong>
            <span>策略成交，不发送实盘委托</span>
          </div>
        </div>
      </aside>
    </>
  );
}

function Header({
  snapshot,
  runtime,
  sourceOptions,
  source,
  setSource,
  onStart,
  onStop,
  busy,
  wsConnected,
  openMenu,
}) {
  const up = snapshot.quote?.changePct >= 0;
  const running = runtime.running;
  return (
    <header className="topbar">
      <button className="icon-button mobile-menu" onClick={openMenu} title="打开菜单">
        <Menu size={20} />
      </button>
      <div className="instrument">
        <div>
          <span className="eyebrow">{snapshot.symbol?.code}</span>
          <h1>{snapshot.symbol?.name}</h1>
        </div>
        <div className={`live-price ${up ? "positive" : "negative"}`}>
          <strong>{snapshot.quote?.price ? number(snapshot.quote.price) : "--.--"}</strong>
          <span>{pct(snapshot.quote?.changePct)}</span>
        </div>
      </div>
      <div className="topbar-actions">
        <div className="connection-strip">
          <span className={`connection-dot ${wsConnected ? "online" : ""}`} />
          <span>{wsConnected ? "实时通道正常" : "实时通道断开"}</span>
        </div>
        <div className="select-wrap">
          <Radio size={15} />
          <select value={source} disabled={running || busy} onChange={(event) => setSource(event.target.value)}>
            {sourceOptions.map((option) => (
              <option value={option.id} key={option.id}>{option.label}</option>
            ))}
          </select>
          <ChevronDown size={14} />
        </div>
        <div className={`runtime-chip status-${String(snapshot.status).toLowerCase()}`}>
          <span />
          {statusLabel(snapshot.status)}
        </div>
        {running ? (
          <button className="button button-danger" onClick={onStop} disabled={busy}>
            <Square size={15} fill="currentColor" />
            停止
          </button>
        ) : (
          <button className="button button-primary" onClick={onStart} disabled={busy}>
            {busy ? <RefreshCw size={16} className="spin" /> : <Play size={16} fill="currentColor" />}
            启动
          </button>
        )}
      </div>
    </header>
  );
}

function MetricCard({ label, value, note, icon: Icon, tone = "neutral", progress }) {
  return (
    <article className="metric-card">
      <div className={`metric-icon ${tone}`}><Icon size={18} /></div>
      <div className="metric-copy">
        <span>{label}</span>
        <strong className={tone}>{value}</strong>
        <small>{note}</small>
      </div>
      {progress != null && (
        <div className="mini-progress">
          <span style={{ width: `${Math.max(0, Math.min(100, progress * 100))}%` }} />
        </div>
      )}
    </article>
  );
}

function PriceChart({ snapshot }) {
  const points = snapshot.chart || [];
  const width = 900;
  const height = 340;
  const pad = { left: 58, right: 26, top: 26, bottom: 36 };
  const values = points.flatMap((point) => [point.price, point.vwap, point.localVwap].filter((item) => item > 0));
  const fallbackPrice = snapshot.quote?.price || 1;
  let low = values.length ? Math.min(...values) : fallbackPrice * 0.99;
  let high = values.length ? Math.max(...values) : fallbackPrice * 1.01;
  if (high - low < 0.01) {
    low -= 0.02;
    high += 0.02;
  }
  const rangePad = (high - low) * 0.12;
  low -= rangePad;
  high += rangePad;
  const x = (index) => pad.left + (index / Math.max(points.length - 1, 1)) * (width - pad.left - pad.right);
  const y = (value) => pad.top + ((high - value) / (high - low)) * (height - pad.top - pad.bottom);
  const pathFor = (key) => points
    .filter((point) => Number(point[key]) > 0)
    .map((point, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(point[key]).toFixed(1)}`)
    .join(" ");
  const gridValues = Array.from({ length: 5 }, (_, index) => low + ((high - low) * index) / 4).reverse();
  const latest = points.at(-1);
  return (
    <section className="panel chart-panel">
      <div className="panel-head">
        <div>
          <span className="section-kicker">INTRADAY</span>
          <h2>实时价格与成交轨迹</h2>
        </div>
        <div className="chart-legend">
          <span><i className="legend-price" />价格</span>
          <span><i className="legend-vwap" />日 VWAP</span>
          <span><i className="legend-local" />30m VWAP</span>
        </div>
      </div>
      <div className="chart-summary">
        <span>最新 <strong>{latest ? number(latest.price) : "--"}</strong></span>
        <span>日 VWAP <strong>{snapshot.quote?.vwap ? number(snapshot.quote.vwap) : "--"}</strong></span>
        <span>30m VWAP <strong>{snapshot.quote?.localVwap ? number(snapshot.quote.localVwap) : "--"}</strong></span>
        <span>Tick <strong>{snapshot.feed?.lastTick || "--"}</strong></span>
      </div>
      <div className="chart-canvas">
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="实时价格折线图">
          {gridValues.map((value) => (
            <g key={value}>
              <line x1={pad.left} x2={width - pad.right} y1={y(value)} y2={y(value)} className="grid-line" />
              <text x={pad.left - 10} y={y(value) + 4} textAnchor="end" className="axis-label">{value.toFixed(2)}</text>
            </g>
          ))}
          {points.length > 1 ? (
            <>
              <path d={pathFor("vwap")} className="chart-line vwap-line" />
              <path d={pathFor("localVwap")} className="chart-line local-line" />
              <path d={pathFor("price")} className="chart-line price-line" />
              {points.map((point, index) => point.trade && (
                <g key={`${point.time}-${index}`} className={point.trade.side === "BUY" ? "trade-buy" : "trade-sell"}>
                  <circle cx={x(index)} cy={y(point.price)} r="7" />
                  <text x={x(index)} y={y(point.price) - 12} textAnchor="middle">
                    {point.trade.side === "BUY" ? "B" : "S"}
                  </text>
                </g>
              ))}
            </>
          ) : (
            <g>
              <text x={width / 2} y={height / 2 - 8} textAnchor="middle" className="chart-empty-title">等待实时行情</text>
              <text x={width / 2} y={height / 2 + 18} textAnchor="middle" className="chart-empty-copy">启动后将显示价格、VWAP 与成交标记</text>
            </g>
          )}
          <text x={pad.left} y={height - 12} className="axis-label">{points[0]?.time || "09:30"}</text>
          <text x={width - pad.right} y={height - 12} textAnchor="end" className="axis-label">{latest?.time || "15:00"}</text>
        </svg>
      </div>
    </section>
  );
}

function DecisionPanel({ snapshot }) {
  const decision = snapshot.decision || {};
  const action = decision.action || "HOLD";
  const signals = [...(snapshot.signals || [])].sort((a, b) => b.progress - a.progress).slice(0, 4);
  return (
    <section className="panel decision-panel">
      <div className="panel-head">
        <div>
          <span className="section-kicker">STRATEGY</span>
          <h2>当前策略决策</h2>
        </div>
        <span className={`action-badge action-${action.toLowerCase()}`}>{actionLabel(action)}</span>
      </div>
      <div className={`decision-hero action-${action.toLowerCase()}`}>
        <span>{decision.state === "filled" ? "最新执行结果" : "当前动作"}</span>
        <strong>{decision.headline || actionLabel(action)}</strong>
        <p>{decision.reason || "等待策略信号。"}</p>
      </div>
      {decision.restrictions?.length > 0 && (
        <div className="restriction-list">
          {decision.restrictions.map((item) => (
            <div key={item}><ShieldCheck size={15} />{item}</div>
          ))}
        </div>
      )}
      <div className="signal-title">
        <span>最接近触发</span>
        <small>分数 / 阈值</small>
      </div>
      <div className="signal-list">
        {signals.length ? signals.map((signal) => (
          <div className={`signal-row signal-${signal.state}`} key={signal.key}>
            <div>
              <span className={`direction-mark ${signal.direction.toLowerCase()}`} />
              <strong>{signal.label}</strong>
              <small title={signal.reason || ""}>{signalStateLabel(signal.state)}</small>
            </div>
            <span>{signal.score.toFixed(2)} / {signal.threshold.toFixed(2)}</span>
            <div className="signal-progress"><i style={{ width: `${signal.progress * 100}%` }} /></div>
          </div>
        )) : (
          <div className="empty-compact">启动行情后显示实时信号强度</div>
        )}
      </div>
    </section>
  );
}

function PositionPanel({ snapshot }) {
  const account = snapshot.account || {};
  const actual = Number(account.positionPct || 0);
  const target = Number(account.targetPct || 0);
  const floor = Number(snapshot.regime?.floorPct ?? account.floorPct ?? 0);
  const ceiling = Number(snapshot.regime?.ceilingPct ?? account.ceilingPct ?? 1);
  return (
    <section className="panel position-panel">
      <div className="panel-head compact">
        <div>
          <span className="section-kicker">POSITION</span>
          <h2>账户与仓位</h2>
        </div>
        <span className={`mode-badge mode-${String(account.mode).toLowerCase()}`}>{account.mode || "--"}</span>
      </div>
      <div className="position-numbers">
        <div><span>持股</span><strong>{number(account.shares, 0)} 股</strong></div>
        <div><span>可用现金</span><strong>{money(account.cash)}</strong></div>
      </div>
      <div className="position-scale">
        <div className="position-scale-head">
          <span>实际仓位 <strong>{pct(actual, 1, false)}</strong></span>
          <span>目标 <strong>{pct(target, 1, false)}</strong></span>
        </div>
        <div className="position-track">
          <i className="allowed-band" style={{ left: `${floor * 100}%`, width: `${Math.max(0, ceiling - floor) * 100}%` }} />
          <i className="actual-fill" style={{ width: `${actual * 100}%` }} />
          <i className="target-pin" style={{ left: `${target * 100}%` }} />
        </div>
        <div className="position-axis"><span>0%</span><span>允许 {pct(floor, 0, false)}–{pct(ceiling, 0, false)}</span><span>100%</span></div>
      </div>
      <div className="position-meta">
        <div><Clock3 size={15} /><span>今日交易</span><strong>{account.dayTradeCount || 0} / {account.maxDayTrades || 0}</strong></div>
        <div><Activity size={15} /><span>市场状态</span><strong>{snapshot.regime?.name || "--"}</strong></div>
        <div><Gauge size={15} /><span>日内 T 周期</span><strong>{account.localCycle || "none"}</strong></div>
      </div>
    </section>
  );
}

function OrderBook({ snapshot }) {
  const orderbook = snapshot.orderbook || { asks: [], bids: [] };
  const asks = orderbook.asks?.length ? orderbook.asks : Array.from({ length: 5 }, (_, index) => ({ level: 5 - index, price: 0, volume: 0 }));
  const bids = orderbook.bids?.length ? orderbook.bids : Array.from({ length: 5 }, (_, index) => ({ level: index + 1, price: 0, volume: 0 }));
  const maxVolume = Math.max(1, ...asks.map((item) => item.volume), ...bids.map((item) => item.volume));
  return (
    <section className="panel orderbook-panel">
      <div className="panel-head compact">
        <div>
          <span className="section-kicker">ORDER BOOK</span>
          <h2>买卖五档</h2>
        </div>
        <span className={`imbalance ${orderbook.imbalance >= 0 ? "positive" : "negative"}`}>
          不平衡 {pct(orderbook.imbalance, 1)}
        </span>
      </div>
      <div className="book-table">
        {asks.map((item) => (
          <div className="book-row ask" key={`ask-${item.level}`}>
            <span>卖 {item.level}</span>
            <strong>{item.price ? number(item.price) : "--"}</strong>
            <span>{item.volume ? number(item.volume, 0) : "--"}</span>
            <i style={{ width: `${(item.volume / maxVolume) * 100}%` }} />
          </div>
        ))}
        <div className="book-mid">
          <span>最新</span>
          <strong>{snapshot.quote?.price ? number(snapshot.quote.price) : "--"}</strong>
          <small>{pct(snapshot.quote?.changePct)}</small>
        </div>
        {bids.map((item) => (
          <div className="book-row bid" key={`bid-${item.level}`}>
            <span>买 {item.level}</span>
            <strong>{item.price ? number(item.price) : "--"}</strong>
            <span>{item.volume ? number(item.volume, 0) : "--"}</span>
            <i style={{ width: `${(item.volume / maxVolume) * 100}%` }} />
          </div>
        ))}
      </div>
    </section>
  );
}

function TradeTable({ trades, compact = false, query = "", side = "ALL" }) {
  const filtered = trades.filter((trade) => {
    if (side !== "ALL" && trade.side !== side) return false;
    const haystack = `${trade.reason} ${trade.detail} ${trade.timestamp}`.toLowerCase();
    return haystack.includes(query.toLowerCase());
  });
  return (
    <div className="table-wrap">
      <table className="trade-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>动作</th>
            <th>价格</th>
            <th>数量</th>
            <th>仓位变化</th>
            <th>状态</th>
            <th>触发原因</th>
            {!compact && <th>金额</th>}
          </tr>
        </thead>
        <tbody>
          {filtered.length ? filtered.slice(0, compact ? 12 : 300).map((trade) => (
            <tr key={trade.id}>
              <td>{trade.timestamp?.replace("T", " ") || "--"}</td>
              <td><span className={`side-pill ${trade.side.toLowerCase()}`}>{actionLabel(trade.side)}</span></td>
              <td className="numeric">{number(trade.price)}</td>
              <td className="numeric">{number(trade.shares, 0)}</td>
              <td className="numeric">{pct(trade.positionBefore, 1, false)} → {pct(trade.positionAfter, 1, false)}</td>
              <td><span className="filled-state"><CheckCircle2 size={13} />{trade.statusLabel}</span></td>
              <td className="reason-cell">
                <strong>{trade.reason || "--"}</strong>
                {trade.detail && <small>{trade.detail}</small>}
              </td>
              {!compact && <td className="numeric">{money(trade.amount)}</td>}
            </tr>
          )) : (
            <tr><td colSpan={compact ? 7 : 8}><div className="empty-table">暂无符合条件的交易记录</div></td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function BottomWorkspace({ trades, logs, factors }) {
  const [tab, setTab] = useState("trades");
  const tabs = [
    { id: "trades", label: "交易记录", count: trades.length },
    { id: "factors", label: "策略依据", count: factors.length },
    { id: "events", label: "系统事件", count: logs.filter((item) => item.level !== "info").length },
    { id: "logs", label: "原始日志", count: logs.length },
  ];
  return (
    <section className="panel workspace-panel">
      <div className="workspace-tabs">
        {tabs.map((item) => (
          <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>
            {item.label}<span>{item.count}</span>
          </button>
        ))}
      </div>
      {tab === "trades" && <TradeTable trades={trades} compact />}
      {tab === "factors" && (
        <div className="factor-grid">
          {factors.length ? factors.map((factor) => (
            <div className="factor-item" key={factor.key}>
              <span>{factor.label}</span>
              <strong className={factor.tone}>{factor.value}</strong>
            </div>
          )) : <div className="empty-compact">启动行情后显示策略因子</div>}
        </div>
      )}
      {tab === "events" && (
        <div className="event-list">
          {logs.filter((item) => item.level !== "info").slice(0, 60).map((item, index) => (
            <div className={`event-row ${item.level}`} key={`${item.timestamp}-${index}`}>
              {item.level === "error" ? <AlertTriangle size={15} /> : <Activity size={15} />}
              <span>{timeText(item.timestamp)}</span>
              <p>{item.message}</p>
            </div>
          ))}
          {!logs.some((item) => item.level !== "info") && <div className="empty-compact">当前没有异常事件</div>}
        </div>
      )}
      {tab === "logs" && (
        <div className="log-view">
          {logs.slice(0, 200).map((item, index) => (
            <div key={`${item.timestamp}-${index}`}><span>{timeText(item.timestamp)}</span><code>{item.message}</code></div>
          ))}
          {!logs.length && <div className="empty-compact">暂无系统日志</div>}
        </div>
      )}
    </section>
  );
}

function MonitorPage({ snapshot, trades, logs }) {
  const account = snapshot.account || {};
  const positivePnl = account.pnl >= 0;
  return (
    <main className="page monitor-page">
      {snapshot.status === "PAUSE" && (
        <div className="notice-bar"><Clock3 size={17} /><span>当前为非交易时段，系统保留状态并等待下一个交易窗口。</span></div>
      )}
      <div className="metric-grid">
        <MetricCard
          label="总权益"
          value={money(account.equity)}
          note={`累计盈亏 ${money(account.pnl)}`}
          icon={WalletCards}
          tone={positivePnl ? "positive" : "negative"}
        />
        <MetricCard
          label="当前决策"
          value={actionLabel(snapshot.decision?.action)}
          note={snapshot.decision?.headline || "等待策略"}
          icon={Zap}
          tone={snapshot.decision?.action === "BUY" ? "positive" : snapshot.decision?.action === "SELL" ? "negative" : "neutral"}
        />
        <MetricCard
          label="实际 / 目标仓位"
          value={`${pct(account.positionPct, 1, false)} / ${pct(account.targetPct, 1, false)}`}
          note={`允许 ${pct(snapshot.regime?.floorPct, 0, false)}–${pct(snapshot.regime?.ceilingPct, 0, false)}`}
          icon={Gauge}
          progress={account.positionPct}
        />
        <MetricCard
          label="市场状态"
          value={snapshot.regime?.name || "--"}
          note={snapshot.regime?.detail || "等待实时判断"}
          icon={Activity}
        />
        <MetricCard
          label="行情状态"
          value={snapshot.feed?.label || "--"}
          note={`${snapshot.feed?.fallback ? "已启用备用源 · " : ""}${snapshot.feed?.lastTick || "未收到 Tick"}`}
          icon={Radio}
          tone={snapshot.status === "RUNNING" ? "positive" : "neutral"}
        />
      </div>
      <div className="dashboard-grid">
        <PriceChart snapshot={snapshot} />
        <DecisionPanel snapshot={snapshot} />
        <PositionPanel snapshot={snapshot} />
        <OrderBook snapshot={snapshot} />
      </div>
      <BottomWorkspace trades={trades} logs={logs} factors={snapshot.factors || []} />
    </main>
  );
}

function BacktestPage({ backtest, onRunBacktest, runBusy, runMessage, runtimeRunning }) {
  const runDisabled = runBusy || runtimeRunning;
  const statusMessage = runtimeRunning ? "实时监控运行中，请先停止后再回测" : runMessage;
  const runButton = (
    <button
      className="button button-primary"
      onClick={onRunBacktest}
      disabled={runDisabled}
      title={runtimeRunning ? "实时监控运行中，请先停止后再回测" : "使用当前本地行情重新回测"}
    >
      <RefreshCw size={16} className={runBusy ? "spin" : ""} />
      {runBusy ? "回测中" : "重新回测"}
    </button>
  );
  if (!backtest?.available) {
    return (
      <main className="page">
        <div className="page-title">
          <div><span className="section-kicker">BACKTEST</span><h2>回测分析</h2><p>当前没有可用的回测汇总</p></div>
          <div className="page-actions">{runButton}</div>
        </div>
        {statusMessage && <div className="notice-bar"><RefreshCw size={16} />{statusMessage}</div>}
        <div className="panel empty-page">没有找到回测汇总，请先运行一次回测。</div>
      </main>
    );
  }
  const comparisons = [
    { label: "V6 策略", value: backtest.strategy_return, color: "strategy" },
    { label: "70% 基准", value: backtest.benchmark_return, color: "benchmark" },
    { label: "满仓持有", value: backtest.full_hold_benchmark_return, color: "hold" },
  ];
  const maxReturn = Math.max(...comparisons.map((item) => item.value), 0.01);
  return (
    <main className="page">
      <div className="page-title">
        <div><span className="section-kicker">BACKTEST</span><h2>回测分析</h2><p>{backtest.start_date} 至 {backtest.end_date}</p></div>
        <div className="page-actions">
          {statusMessage && <span className="run-state">{statusMessage}</span>}
          <span className="subtle-chip">{number(backtest.data_rows, 0)} 条数据 · {backtest.trade_count} 笔交易</span>
          {runButton}
        </div>
      </div>
      <div className="summary-grid">
        <MetricCard label="策略收益" value={pct(backtest.strategy_return)} note={`最终资产 ${money(backtest.strategy_final_asset)}`} icon={TrendingUp} tone="positive" />
        <MetricCard label="超额收益" value={pct(backtest.alpha)} note="相对 70% 仓位基准" icon={Zap} tone="positive" />
        <MetricCard label="最大回撤" value={pct(backtest.max_drawdown, 2, false)} note={`基准 ${pct(backtest.benchmark_max_drawdown, 2, false)}`} icon={TrendingDown} tone="negative" />
        <MetricCard label="换手率" value={number(backtest.turnover, 2)} note={`${backtest.trade_count} 笔策略交易`} icon={RefreshCw} />
      </div>
      <div className="two-column">
        <section className="panel comparison-panel">
          <div className="panel-head"><div><span className="section-kicker">PERFORMANCE</span><h2>收益比较</h2></div></div>
          <div className="comparison-bars">
            {comparisons.map((item) => (
              <div className="comparison-row" key={item.label}>
                <span>{item.label}</span>
                <div><i className={item.color} style={{ width: `${(item.value / maxReturn) * 100}%` }} /></div>
                <strong>{pct(item.value)}</strong>
              </div>
            ))}
          </div>
        </section>
        <section className="panel detail-panel">
          <div className="panel-head"><div><span className="section-kicker">DETAILS</span><h2>运行摘要</h2></div></div>
          <dl className="detail-list">
            <div><dt>策略版本</dt><dd>{backtest.strategy_variant}</dd></div>
            <div><dt>初始策略仓位</dt><dd>{pct(backtest.initial_strategy_target_pct, 0, false)}</dd></div>
            <div><dt>最终持股</dt><dd>{number(backtest.final_shares, 0)} 股</dd></div>
            <div><dt>最终仓位</dt><dd>{pct(backtest.final_position_pct, 1, false)}</dd></div>
            <div><dt>盘口降级次数</dt><dd>{backtest.orderbook_fallback_count}</dd></div>
            <div><dt>涨跌停跳过</dt><dd>{backtest.limit_skip_count}</dd></div>
          </dl>
        </section>
      </div>
      {backtest.known_data_quality_warnings?.length > 0 && (
        <section className="panel warning-panel">
          <div className="panel-head compact"><div><span className="section-kicker">DATA QUALITY</span><h2>已知数据提示</h2></div></div>
          {backtest.known_data_quality_warnings.map((warning) => <div className="warning-row" key={warning}><AlertTriangle size={16} />{warning}</div>)}
        </section>
      )}
    </main>
  );
}

function TradesPage({ trades }) {
  const [query, setQuery] = useState("");
  const [side, setSide] = useState("ALL");
  const buyCount = trades.filter((item) => item.side === "BUY").length;
  const sellCount = trades.filter((item) => item.side === "SELL").length;
  return (
    <main className="page">
      <div className="page-title">
        <div><span className="section-kicker">LEDGER</span><h2>交易记录</h2><p>策略成交与触发依据的结构化流水</p></div>
        <div className="trade-stats">
          <span><i className="buy-dot" />买入 {buyCount}</span>
          <span><i className="sell-dot" />卖出 {sellCount}</span>
        </div>
      </div>
      <section className="panel">
        <div className="table-toolbar">
          <label className="search-box"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索原因、详情或时间" /></label>
          <div className="segmented">
            {["ALL", "BUY", "SELL"].map((item) => (
              <button key={item} className={side === item ? "active" : ""} onClick={() => setSide(item)}>
                {item === "ALL" ? "全部" : actionLabel(item)}
              </button>
            ))}
          </div>
        </div>
        <TradeTable trades={trades} query={query} side={side} />
      </section>
    </main>
  );
}

function DataPage({ dataStatus }) {
  const warnings = dataStatus?.knownWarnings || [];
  return (
    <main className="page">
      <div className="page-title">
        <div><span className="section-kicker">DATA</span><h2>数据管理</h2><p>本地行情覆盖、运行状态与质量提示</p></div>
      </div>
      <div className="summary-grid">
        <MetricCard label="行情文件" value={number(dataStatus?.fileCount, 0)} note={dataStatus?.directory || "--"} icon={Database} />
        <MetricCard label="覆盖起点" value={dataStatus?.firstDate || "--"} note="最早本地交易日" icon={Clock3} />
        <MetricCard label="覆盖终点" value={dataStatus?.lastDate || "--"} note="最新本地交易日" icon={CheckCircle2} tone="positive" />
        <MetricCard label="数据体积" value={bytes(dataStatus?.totalBytes)} note={`更新 ${dataStatus?.latestModifiedAt?.replace("T", " ") || "--"}`} icon={Server} />
      </div>
      <div className="two-column">
        <section className="panel detail-panel">
          <div className="panel-head"><div><span className="section-kicker">RUNTIME FILES</span><h2>运行文件</h2></div></div>
          <div className="health-list">
            <div><CheckCircle2 className={dataStatus?.runtimeStateExists ? "positive" : "muted"} /><span>策略状态快照</span><strong>{dataStatus?.runtimeStateExists ? "已生成" : "尚未生成"}</strong></div>
            <div><CheckCircle2 className={dataStatus?.runtimeTradesExists ? "positive" : "muted"} /><span>实时交易流水</span><strong>{dataStatus?.runtimeTradesExists ? "已生成" : "尚未生成"}</strong></div>
          </div>
        </section>
        <section className="panel warning-panel">
          <div className="panel-head"><div><span className="section-kicker">QUALITY</span><h2>数据质量提示</h2></div></div>
          {warnings.length ? warnings.map((warning) => (
            <div className="warning-row" key={warning}><AlertTriangle size={16} />{warning}</div>
          )) : <div className="health-empty"><CheckCircle2 size={20} />当前没有已知数据质量警告</div>}
        </section>
      </div>
    </main>
  );
}

function SettingsPage({ system, sourceOptions, source, setSource, runtime }) {
  return (
    <main className="page">
      <div className="page-title">
        <div><span className="section-kicker">SYSTEM</span><h2>系统设置</h2><p>运行环境与只读配置概览</p></div>
      </div>
      <div className="two-column settings-grid">
        <section className="panel settings-panel">
          <div className="panel-head"><div><span className="section-kicker">RUNTIME</span><h2>运行设置</h2></div></div>
          <label className="setting-row">
            <span><Radio size={17} /><span><strong>默认行情源</strong><small>启动前可切换，运行中锁定</small></span></span>
            <select value={source} disabled={runtime.running} onChange={(event) => setSource(event.target.value)}>
              {sourceOptions.map((option) => <option value={option.id} key={option.id}>{option.label}</option>)}
            </select>
          </label>
          <div className="setting-row">
            <span><ShieldCheck size={17} /><span><strong>交易模式</strong><small>当前策略账户模式</small></span></span>
            <span className="subtle-chip">模拟交易</span>
          </div>
          <div className="setting-row">
            <span><Activity size={17} /><span><strong>策略版本</strong><small>服务端实际运行模块</small></span></span>
            <strong>{system?.strategy || "--"}</strong>
          </div>
        </section>
        <section className="panel settings-panel">
          <div className="panel-head"><div><span className="section-kicker">ARCHITECTURE</span><h2>服务架构</h2></div></div>
          <dl className="detail-list">
            <div><dt>后端服务</dt><dd>{system?.backend || "aiohttp"}</dd></div>
            <div><dt>实时传输</dt><dd>{system?.transport || "WebSocket"}</dd></div>
            <div><dt>标的</dt><dd>{system?.symbol || "002796.SZ"}</dd></div>
            <div><dt>数据目录</dt><dd className="path-text">{system?.dataDirectory || "--"}</dd></div>
            <div><dt>回测目录</dt><dd className="path-text">{system?.backtestDirectory || "--"}</dd></div>
          </dl>
        </section>
      </div>
      <section className="panel architecture-note">
        <BookOpen size={20} />
        <div><strong>职责边界</strong><p>React 只负责展示和操作；aiohttp 负责 API、WebSocket 与生命周期管理；CombinedStrategyV6、状态恢复、行情适配及交易流水继续由 Python 服务层统一执行。</p></div>
      </section>
    </main>
  );
}

function TradeToast({ trade, onClose }) {
  if (!trade) return null;
  return (
    <aside className={`trade-toast ${trade.side.toLowerCase()}`}>
      <button className="toast-close" onClick={onClose} title="关闭通知"><X size={15} /></button>
      <div className="toast-icon">{trade.side === "BUY" ? <TrendingUp size={20} /> : <TrendingDown size={20} />}</div>
      <div className="toast-copy">
        <span>{trade.side === "BUY" ? "买入成交" : "卖出成交"} · {trade.time}</span>
        <strong>{number(trade.shares, 0)} 股 × {money(trade.price, 2)}</strong>
        <p>{money(trade.amount)} · 仓位 {pct(trade.positionBefore, 1, false)} → {pct(trade.positionAfter, 1, false)}</p>
        <small>{trade.reason}</small>
      </div>
    </aside>
  );
}

export default function App() {
  const [page, setPage] = useState("monitor");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [snapshot, setSnapshot] = useState(FALLBACK_SNAPSHOT);
  const [runtime, setRuntime] = useState({ running: false, source: "tencent", status: "LOADING" });
  const [trades, setTrades] = useState([]);
  const [logs, setLogs] = useState([]);
  const [backtest, setBacktest] = useState(null);
  const [dataStatus, setDataStatus] = useState(null);
  const [sourceOptions, setSourceOptions] = useState([{ id: "tencent", label: "现有接口" }, { id: "qmt", label: "QMT" }]);
  const [source, setSource] = useState("tencent");
  const [system, setSystem] = useState({});
  const [busy, setBusy] = useState(false);
  const [backtestBusy, setBacktestBusy] = useState(false);
  const [backtestMessage, setBacktestMessage] = useState("");
  const [wsConnected, setWsConnected] = useState(false);
  const [toast, setToast] = useState(null);
  const toastTimer = useRef(null);

  const applyBootstrap = useCallback((payload) => {
    setRuntime(payload.runtime || {});
    setSnapshot(payload.snapshot || FALLBACK_SNAPSHOT);
    setTrades(payload.trades || []);
    setLogs(payload.logs || []);
    setBacktest(payload.backtest || null);
    setDataStatus(payload.dataStatus || null);
    setSourceOptions(payload.sourceOptions || []);
    setSystem(payload.system || {});
    setSource(payload.runtime?.source || payload.snapshot?.feed?.requestedSource || "tencent");
  }, []);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/bootstrap")
      .then((response) => response.json())
      .then((payload) => {
        if (!cancelled) applyBootstrap(payload);
      })
      .catch(() => {
        if (!cancelled) setSnapshot((current) => ({ ...current, status: "STOPPED" }));
      });
    return () => { cancelled = true; };
  }, [applyBootstrap]);

  useEffect(() => {
    let socket;
    let retryTimer;
    let closed = false;
    const connect = () => {
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      socket = new WebSocket(`${protocol}//${window.location.host}/ws`);
      socket.onopen = () => setWsConnected(true);
      socket.onclose = () => {
        setWsConnected(false);
        if (!closed) retryTimer = window.setTimeout(connect, 1500);
      };
      socket.onerror = () => socket.close();
      socket.onmessage = (event) => {
        if (event.data === "pong") return;
        const message = JSON.parse(event.data);
        if (message.type === "bootstrap") {
          applyBootstrap(message);
        } else if (message.type === "snapshot") {
          setSnapshot(message);
          setRuntime((current) => ({ ...current, status: message.status, running: ["STARTING", "RUNNING", "PAUSE", "STOPPING"].includes(message.status) }));
        } else if (message.type === "runtime") {
          setRuntime(message);
          setSnapshot((current) => ({ ...current, status: message.status }));
        } else if (message.type === "log") {
          setLogs((current) => [message.log, ...current].slice(0, 500));
        } else if (message.type === "trade") {
          setTrades((current) => [message.trade, ...current.filter((item) => item.id !== message.trade.id)].slice(0, 300));
          setToast(message.trade);
          window.clearTimeout(toastTimer.current);
          toastTimer.current = window.setTimeout(() => setToast(null), 8000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retryTimer);
      window.clearTimeout(toastTimer.current);
      socket?.close();
    };
  }, [applyBootstrap]);

  const postRuntime = async (action) => {
    setBusy(true);
    try {
      const response = await fetch(`/api/runtime/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source }),
      });
      const payload = await response.json();
      setRuntime(payload);
      setSnapshot((current) => ({ ...current, status: payload.status }));
    } finally {
      setBusy(false);
    }
  };

  const runBacktest = async () => {
    setBacktestBusy(true);
    setBacktestMessage("正在使用当前本地行情重新回测");
    try {
      const response = await fetch("/api/backtest/run", { method: "POST" });
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "回测失败");
      }
      setBacktest(payload.backtest || null);
      setTrades(payload.trades || []);
      setDataStatus(payload.dataStatus || null);
      if (payload.snapshot) {
        setSnapshot(payload.snapshot);
      }
      setBacktestMessage(`已刷新：${number(payload.backtest?.trade_count, 0)} 笔交易`);
    } catch (error) {
      setBacktestMessage(error?.message || "回测失败");
    } finally {
      setBacktestBusy(false);
    }
  };

  const pageContent = useMemo(() => {
    if (page === "backtest") {
      return (
        <BacktestPage
          backtest={backtest}
          onRunBacktest={runBacktest}
          runBusy={backtestBusy}
          runMessage={backtestMessage}
          runtimeRunning={runtime.running}
        />
      );
    }
    if (page === "trades") return <TradesPage trades={trades} />;
    if (page === "data") return <DataPage dataStatus={dataStatus} />;
    if (page === "settings") return <SettingsPage system={system} sourceOptions={sourceOptions} source={source} setSource={setSource} runtime={runtime} />;
    return <MonitorPage snapshot={snapshot} trades={trades} logs={logs} />;
  }, [page, backtest, backtestBusy, backtestMessage, trades, dataStatus, system, sourceOptions, source, runtime, snapshot, logs]);

  return (
    <div className="app-shell">
      <Sidebar page={page} setPage={setPage} open={sidebarOpen} setOpen={setSidebarOpen} />
      <div className="app-main">
        <Header
          snapshot={snapshot}
          runtime={runtime}
          sourceOptions={sourceOptions}
          source={source}
          setSource={setSource}
          onStart={() => postRuntime("start")}
          onStop={() => postRuntime("stop")}
          busy={busy}
          wsConnected={wsConnected}
          openMenu={() => setSidebarOpen(true)}
        />
        {pageContent}
      </div>
      <TradeToast trade={toast} onClose={() => setToast(null)} />
    </div>
  );
}
