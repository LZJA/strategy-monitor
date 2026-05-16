import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  api,
  PositionChange,
  PositionChart,
  Position,
  PositionQuote,
  ScanResult,
  Signal,
  SignalChart,
  Snapshot,
  User
} from "../api/client";
import { SignalKlineChart } from "./SignalKlineChart";

type Props = {
  user: User;
  onLogout: () => void;
};

type ModuleKey = "dashboard" | "strategy" | "account";
type TimerKind = "quotes" | "scan";

type PositionRow = {
  key: string;
  symbol: string;
  quantity: string;
  costPrice: string;
  name?: string | null;
  latestPrice?: number | null;
};

const money = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, style: "percent" });
const visibleSignalTypes = new Set(["matched", "confirmed", "命中", "确认", "突破", "突破回踩确认"]);

function localDateString(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const today = localDateString();

function formatMoney(value: number | null | undefined) {
  return value == null ? "--" : money.format(value);
}

function formatPercent(value: number | null | undefined) {
  return value == null || Number.isNaN(value) ? "--" : percent.format(value);
}

function toNumber(value: string) {
  return value === "" ? 0 : Number(value);
}

function newPositionRow(): PositionRow {
  return {
    key: crypto.randomUUID(),
    symbol: "",
    quantity: "",
    costPrice: ""
  };
}

function normalizeSymbol(value: string) {
  return value.trim().toUpperCase();
}

function formatSignalType(value: string) {
  const labels: Record<string, string> = {
    matched: "命中",
    watch: "观察",
    confirmed: "确认",
    imported: "导入"
  };
  return labels[value] ?? value;
}

export function Dashboard({ user, onLogout }: Props) {
  const [activeModule, setActiveModule] = useState<ModuleKey>("dashboard");
  const [signals, setSignals] = useState<Signal[]>([]);
  const [allSignals, setAllSignals] = useState<Signal[]>([]);
  const [snapshots, setSnapshots] = useState<Snapshot[]>([]);
  const [changes, setChanges] = useState<PositionChange[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(null);
  const [signalChart, setSignalChart] = useState<SignalChart | null>(null);
  const [positionChart, setPositionChart] = useState<PositionChart | null>(null);
  const [chartLoading, setChartLoading] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [snapshotDate, setSnapshotDate] = useState(today);
  const [cash, setCash] = useState("");
  const [positionRows, setPositionRows] = useState<PositionRow[]>([newPositionRow()]);
  const [quotes, setQuotes] = useState<Record<string, PositionQuote>>({});
  const [liveQuotes, setLiveQuotes] = useState<Record<string, PositionQuote>>({});
  const [strategyDate, setStrategyDate] = useState(today);
  const [strategyName, setStrategyName] = useState("");
  const [timerModal, setTimerModal] = useState<TimerKind | null>(null);
  const [intervalSeconds, setIntervalSeconds] = useState("60");
  const quoteTimerRef = useRef<number | null>(null);
  const scanTimerRef = useRef<number | null>(null);
  const [quoteTimerSeconds, setQuoteTimerSeconds] = useState<number | null>(null);
  const [scanTimerSeconds, setScanTimerSeconds] = useState<number | null>(null);
  const isAdmin = user.role === "admin";

  const selectedSignal = selectedSignalId ? allSignals.find((signal) => signal.id === selectedSignalId) ?? null : null;
  const positionMarketValue = snapshot?.positions.reduce((sum, item) => sum + (item.market_value ?? 0), 0) ?? 0;
  const positionProfitLoss = snapshot?.positions.reduce((sum, item) => sum + (item.profit_loss ?? 0), 0) ?? 0;
  const cashValue = toNumber(cash);

  const strategyDates = useMemo(
    () => Array.from(new Set(allSignals.map((item) => item.signal_date))).sort((a, b) => b.localeCompare(a)),
    [allSignals]
  );
  const strategyNames = useMemo(() => Array.from(new Set(allSignals.map((item) => item.strategy_name))), [allSignals]);
  const filteredSignals = useMemo(() => {
    return allSignals.filter((signal) => {
      if (!visibleSignalTypes.has(signal.signal_type)) {
        return false;
      }
      const dateMatch = !strategyDate || signal.signal_date === strategyDate;
      const strategyMatch = !strategyName || signal.strategy_name === strategyName;
      return dateMatch && strategyMatch;
    });
  }, [allSignals, strategyDate, strategyName]);
  const groupedStrategies = useMemo(() => {
    return filteredSignals.reduce<Record<string, { total: number; dates: Set<string> }>>((acc, signal) => {
      if (!acc[signal.strategy_name]) {
        acc[signal.strategy_name] = { total: 0, dates: new Set() };
      }
      acc[signal.strategy_name].total += 1;
      acc[signal.strategy_name].dates.add(signal.signal_date);
      return acc;
    }, {});
  }, [filteredSignals]);

  const previewRowsBase = positionRows.map((row) => {
    const symbol = normalizeSymbol(row.symbol);
    const quantity = toNumber(row.quantity);
    const costPrice = toNumber(row.costPrice);
    const quote = symbol ? quotes[symbol] : undefined;
    const latestPrice = quote?.latest_price ?? row.latestPrice ?? costPrice;
    const marketValue = latestPrice * quantity;
    const costValue = costPrice * quantity;
    const profitLoss = marketValue - costValue;
    return {
      ...row,
      symbol,
      quantity,
      costPrice,
      name: quote?.name ?? row.name,
      latestPrice,
      marketValue,
      profitLoss,
      profitLossPct: costValue ? profitLoss / costValue : 0
    };
  });
  const previewMarketValue = previewRowsBase.reduce((sum, row) => sum + (row.symbol ? row.marketValue : 0), 0);
  const totalAssetsValue = cashValue + previewMarketValue;
  const previewRows = previewRowsBase.map((row) => ({
    ...row,
    positionPct: totalAssetsValue ? row.marketValue / totalAssetsValue : 0
  }));

  async function refresh() {
    const [nextSignals, nextSnapshot, nextAllSignals, nextSnapshots, nextChanges] = await Promise.all([
      api.todaySignals(),
      api.currentSnapshot(),
      api.signals({ limit: 1000 }),
      api.snapshots(),
      api.positionChanges()
    ]);
    setSignals(nextSignals);
    setAllSignals(nextAllSignals);
    setSnapshots(nextSnapshots);
    setChanges(nextChanges);
    setSnapshot(nextSnapshot);
    if (nextSnapshot) {
      setSnapshotDate(nextSnapshot.snapshot_date);
      setCash(String(nextSnapshot.cash));
      setPositionRows(
        nextSnapshot.positions.length
          ? nextSnapshot.positions.map((position) => ({
              key: crypto.randomUUID(),
              symbol: position.symbol,
              quantity: String(position.quantity),
              costPrice: String(position.cost_price),
              name: position.name,
              latestPrice: position.latest_price
            }))
          : [newPositionRow()]
      );
    }
  }

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof Error ? err.message : "加载失败"));
    return () => {
      if (quoteTimerRef.current) window.clearInterval(quoteTimerRef.current);
      if (scanTimerRef.current) window.clearInterval(scanTimerRef.current);
    };
  }, []);

  useEffect(() => {
    if (strategyDate && !strategyDates.includes(strategyDate)) {
      setStrategyDate(strategyDates[0] ?? "");
    }
  }, [strategyDate, strategyDates]);

  useEffect(() => {
    if (!error && !message) {
      return;
    }
    const timer = window.setTimeout(() => {
      setError("");
      setMessage("");
    }, 4000);
    return () => window.clearTimeout(timer);
  }, [error, message]);

  useEffect(() => {
    if (!scanResult) {
      return;
    }
    const timer = window.setTimeout(() => {
      setScanResult(null);
    }, 4000);
    return () => window.clearTimeout(timer);
  }, [scanResult]);

  useEffect(() => {
    if (!selectedSignalId && !positionChart && allSignals.length) {
      setSelectedSignalId(allSignals[0].id);
    }
  }, [allSignals, positionChart, selectedSignalId]);

  useEffect(() => {
    if (!selectedSignalId) {
      setSignalChart(null);
      return;
    }
    setPositionChart(null);
    setChartLoading(true);
    api
      .signalChart(selectedSignalId)
      .then(setSignalChart)
      .catch((err) => {
        setSignalChart(null);
        setError(err instanceof Error ? err.message : "K 线加载失败");
      })
      .finally(() => setChartLoading(false));
  }, [selectedSignalId]);

  useEffect(() => {
    const symbols = Array.from(new Set(positionRows.map((row) => normalizeSymbol(row.symbol)).filter(Boolean)));
    if (!symbols.length || !snapshotDate) {
      setQuotes({});
      return;
    }
    const timer = window.setTimeout(() => {
      api
        .positionQuotes(symbols, snapshotDate)
        .then((items) => {
          setQuotes(Object.fromEntries(items.map((item) => [item.symbol, item])));
        })
        .catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [positionRows, snapshotDate]);

  async function scanToday() {
    setError("");
    setMessage("");
    setScanning(true);
    try {
      const result = await api.scanToday();
      setScanResult(result);
      await refresh();
      setMessage(`已导入 ${result.imported} 条策略信号`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "扫描失败");
    } finally {
      setScanning(false);
    }
  }

  async function deleteStrategyResults() {
    setError("");
    setMessage("");
    if (!isAdmin) {
      setError("只有管理员可以删除策略结果");
      return;
    }
    if (!strategyDate) {
      setError("请先选择要删除的历史日期");
      return;
    }
    const scope = strategyName ? `${strategyDate} / ${strategyName}` : strategyDate;
    if (!window.confirm(`确认删除 ${scope} 的策略结果吗？`)) {
      return;
    }
    try {
      const result = await api.deleteSignals({ signalDate: strategyDate, strategyName: strategyName || undefined });
      setSelectedSignalId(null);
      setSignalChart(null);
      await refresh();
      setMessage(`已删除 ${result.deleted} 条策略结果`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除策略结果失败");
    }
  }

  async function refreshHoldingQuotes(showMessage = true) {
    const symbols = snapshot?.positions.map((position) => position.symbol).filter(Boolean) ?? [];
    if (!symbols.length) {
      setMessage("当前没有持仓标的可以刷新");
      return;
    }
    const items = await api.positionQuotes(symbols, snapshot?.snapshot_date ?? today);
    setLiveQuotes(Object.fromEntries(items.map((item) => [item.symbol, item])));
    if (showMessage) {
      setMessage(`已更新 ${items.length} 个持仓标的行情`);
    }
  }

  async function openPositionChart(position: Position) {
    setError("");
    setActiveModule("dashboard");
    setChartLoading(true);
    try {
      const chart = await api.positionChart(position.symbol, snapshot?.snapshot_date ?? today);
      setSelectedSignalId(null);
      setSignalChart(null);
      setPositionChart(chart);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载持仓 K 线失败");
    } finally {
      setChartLoading(false);
    }
  }

  function updatePositionRow(key: string, field: "symbol" | "quantity" | "costPrice", value: string) {
    setPositionRows((rows) => rows.map((row) => (row.key === key ? { ...row, [field]: value } : row)));
  }

  function removePositionRow(key: string) {
    setPositionRows((rows) => {
      const nextRows = rows.filter((row) => row.key !== key);
      return nextRows.length ? nextRows : [newPositionRow()];
    });
  }

  async function saveSnapshot(event: FormEvent) {
    event.preventDefault();
    setError("");
    setMessage("");
    if (snapshotDate > today) {
      setError("快照日期不能晚于今天");
      return;
    }
    const positions = previewRows
      .filter((row) => row.symbol)
      .map((row) => ({
        symbol: row.symbol,
        quantity: row.quantity,
        cost_price: row.costPrice,
        name: row.name || undefined,
        latest_price: row.latestPrice ?? undefined
      }));
    try {
      await api.saveSnapshot({
        snapshot_date: snapshotDate,
        total_assets: totalAssetsValue,
        cash: cashValue,
        positions
      });
      await refresh();
      setMessage("账户持仓已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    }
  }

  function openTimer(kind: TimerKind) {
    setTimerModal(kind);
    setIntervalSeconds(kind === "quotes" ? String(quoteTimerSeconds ?? 60) : String(scanTimerSeconds ?? 300));
  }

  function stopTimer(kind: TimerKind) {
    if (kind === "quotes" && quoteTimerRef.current) {
      window.clearInterval(quoteTimerRef.current);
      quoteTimerRef.current = null;
      setQuoteTimerSeconds(null);
    }
    if (kind === "scan" && scanTimerRef.current) {
      window.clearInterval(scanTimerRef.current);
      scanTimerRef.current = null;
      setScanTimerSeconds(null);
    }
  }

  function submitTimer(event: FormEvent) {
    event.preventDefault();
    if (!timerModal) return;
    const seconds = Math.max(5, Number(intervalSeconds) || 60);
    stopTimer(timerModal);
    if (timerModal === "quotes") {
      refreshHoldingQuotes(false).catch((err) => setError(err instanceof Error ? err.message : "行情刷新失败"));
      quoteTimerRef.current = window.setInterval(() => {
        refreshHoldingQuotes(false).catch((err) => setError(err instanceof Error ? err.message : "行情刷新失败"));
      }, seconds * 1000);
      setQuoteTimerSeconds(seconds);
      setMessage(`持仓行情定时刷新已开启，每 ${seconds} 秒一次`);
    } else {
      scanToday();
      scanTimerRef.current = window.setInterval(() => scanToday(), seconds * 1000);
      setScanTimerSeconds(seconds);
      setMessage(`策略实时扫描已开启，每 ${seconds} 秒一次`);
    }
    setTimerModal(null);
  }

  function renderChartPanel() {
    const chartTitle = positionChart
      ? `${positionChart.symbol} ${positionChart.name ?? ""}`
      : selectedSignal
        ? `${selectedSignal.symbol} ${selectedSignal.name ?? ""}`
        : "信号 K 线";
    const chartData = positionChart ? { klines: positionChart.klines, points: [] } : signalChart;
    const priceLines = positionChart
      ? [
          { name: "成本线", value: positionChart.cost_price, color: "#f59e0b" },
          { name: "当前价", value: positionChart.latest_price, color: "#38bdf8" }
        ]
      : [];
    return (
      <section className="terminal-panel chart-panel">
        <div className="panel-title">
          <div>
            <span className="section-kicker">KLINE</span>
            <h2>{chartTitle}</h2>
          </div>
          <span>{positionChart ? "持仓 K 线" : selectedSignal?.strategy_name ?? "等待选择"}</span>
        </div>
        {selectedSignal && (
          <div className="quote-strip">
            <span>日期 {selectedSignal.signal_date}</span>
            <span>收盘 {selectedSignal.close_price ?? "--"}</span>
            <span>最高 {selectedSignal.high_price ?? "--"}</span>
            <span>突破 {selectedSignal.breakout_price ?? "--"}</span>
            <span>止损 {selectedSignal.stop_loss_price ?? "--"}</span>
            <span>止盈 {selectedSignal.take_profit_price ?? "--"}</span>
          </div>
        )}
        {positionChart && (
          <div className="quote-strip">
            <span>快照 {positionChart.snapshot_date}</span>
            <span>成本 {formatMoney(positionChart.cost_price)}</span>
            <span>当前 {formatMoney(positionChart.latest_price)}</span>
          </div>
        )}
        <SignalKlineChart chart={chartData} loading={chartLoading} priceLines={priceLines} />
      </section>
    );
  }

  function renderDashboard() {
    return (
      <>
        <section className="command-row">
          <button type="button" className="trade-button buy" onClick={() => openTimer("quotes")}>
            实时获取持仓标的信息
          </button>
          <button type="button" className="trade-button warn" onClick={() => openTimer("scan")} disabled={!isAdmin}>
            实时扫描策略
          </button>
          {quoteTimerSeconds && (
            <button type="button" className="trade-button muted-button" onClick={() => stopTimer("quotes")}>
              停止行情 {quoteTimerSeconds}s
            </button>
          )}
          {scanTimerSeconds && (
            <button type="button" className="trade-button muted-button" onClick={() => stopTimer("scan")}>
              停止扫描 {scanTimerSeconds}s
            </button>
          )}
        </section>

        <section className="metrics-grid">
          <article>
            <span>总资产</span>
            <strong>{formatMoney(snapshot?.total_assets)}</strong>
            <small>快照 {snapshot?.snapshot_date ?? "--"}</small>
          </article>
          <article>
            <span>持仓市值</span>
            <strong>{formatMoney(positionMarketValue)}</strong>
            <small>现金 {formatMoney(snapshot?.cash)}</small>
          </article>
          <article>
            <span>浮动盈亏</span>
            <strong className={positionProfitLoss >= 0 ? "up" : "down"}>{formatMoney(positionProfitLoss)}</strong>
            <small>{snapshot?.positions.length ?? 0} 个持仓</small>
          </article>
          <article>
            <span>今日信号</span>
            <strong>{signals.length}</strong>
            <small>最新 {signals[0]?.signal_date ?? "--"}</small>
          </article>
        </section>

        <section className="dashboard-grid">
          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">POSITIONS</span>
                <h2>持仓股票</h2>
              </div>
              <span>点击查看 K 线</span>
            </div>
            <div className="table-wrap">
              <table className="market-table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>数量</th>
                    <th>成本</th>
                    <th>最新</th>
                    <th>市值</th>
                    <th>盈亏</th>
                    <th>比例</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot?.positions.map((position) => {
                    const quote = liveQuotes[position.symbol];
                    return (
                      <tr key={position.id} onClick={() => openPositionChart(position)}>
                        <td className="symbol-cell">{position.symbol}</td>
                        <td>{quote?.name ?? position.name ?? "--"}</td>
                        <td>{position.quantity}</td>
                        <td>{formatMoney(position.cost_price)}</td>
                        <td>{formatMoney(quote?.latest_price ?? position.latest_price)}</td>
                        <td>{formatMoney(position.market_value)}</td>
                        <td className={(position.profit_loss ?? 0) >= 0 ? "up" : "down"}>
                          {formatMoney(position.profit_loss)}
                        </td>
                        <td>{formatPercent(position.position_pct)}</td>
                      </tr>
                    );
                  })}
                  {!snapshot?.positions.length && (
                    <tr>
                      <td colSpan={8}>
                        <div className="empty-state">暂无持仓，去账户模块录入一份持仓快照。</div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">SIGNALS</span>
                <h2>信号股票</h2>
              </div>
              <span>{signals.length} 条</span>
            </div>
            <div className="signal-board">
              {signals.slice(0, 18).map((signal) => (
                <button
                  key={signal.id}
                  className={`signal-tile ${selectedSignalId === signal.id ? "active" : ""}`}
                  type="button"
                  onClick={() => setSelectedSignalId(signal.id)}
                >
                  <strong>{signal.symbol}</strong>
                  <span>{signal.name ?? "--"}</span>
                  <small>{signal.strategy_name}</small>
                </button>
              ))}
              {!signals.length && <div className="empty-state signal-empty">暂无今日信号，可以启动实时扫描策略。</div>}
            </div>
          </section>
        </section>

        {renderChartPanel()}
      </>
    );
  }

  function renderStrategy() {
    return (
      <>
        <section className="filter-bar">
          <label>
            历史日期
            <select value={strategyDate} onChange={(event) => setStrategyDate(event.target.value)}>
              <option value="">全部日期</option>
              {strategyDates.map((date) => (
                <option key={date} value={date}>
                  {date}
                </option>
              ))}
            </select>
          </label>
          <label>
            策略名称
            <select value={strategyName} onChange={(event) => setStrategyName(event.target.value)}>
              <option value="">全部策略</option>
              {strategyNames.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="trade-button buy" onClick={scanToday} disabled={!isAdmin || scanning}>
            {scanning ? "扫描中..." : "扫描今日策略"}
          </button>
          <button
            type="button"
            className="trade-button danger"
            onClick={deleteStrategyResults}
            disabled={!isAdmin || !strategyDate || !filteredSignals.length}
          >
            删除筛选结果
          </button>
        </section>

        <section className="strategy-grid">
          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">CACHE</span>
                <h2>历史策略缓存</h2>
              </div>
              <span>{filteredSignals.length} 条结果</span>
            </div>
            <div className="strategy-list">
              {Object.entries(groupedStrategies).map(([name, stat]) => (
                <button key={name} type="button" onClick={() => setStrategyName(name)}>
                  <span>{name}</span>
                  <strong>{stat.total}</strong>
                  <small>{stat.dates.size} 个交易日</small>
                </button>
              ))}
              {!filteredSignals.length && <div className="empty-state">当前筛选条件下没有历史策略结果。</div>}
            </div>
          </section>

          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">DETAIL</span>
                <h2>策略结果详情</h2>
              </div>
              <span>点击行查看 K 线</span>
            </div>
            <div className="table-wrap">
              <table className="market-table">
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>策略</th>
                    <th>类型</th>
                    <th>代码</th>
                    <th>名称</th>
                    <th>收盘</th>
                    <th>突破</th>
                    <th>排名</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredSignals.map((signal) => (
                    <tr
                      key={signal.id}
                      className={selectedSignalId === signal.id ? "selected-row" : ""}
                      onClick={() => setSelectedSignalId(signal.id)}
                    >
                      <td>{signal.signal_date}</td>
                      <td>{signal.strategy_name}</td>
                      <td>{formatSignalType(signal.signal_type)}</td>
                      <td className="symbol-cell">{signal.symbol}</td>
                      <td>{signal.name ?? "--"}</td>
                      <td>{signal.close_price ?? "--"}</td>
                      <td>{signal.breakout_price ?? "--"}</td>
                      <td>{signal.amount_rank ?? "--"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </section>
        {renderChartPanel()}
      </>
    );
  }

  function renderAccount() {
    return (
      <>
        <form className="terminal-panel" onSubmit={saveSnapshot}>
          <div className="panel-title">
            <div>
              <span className="section-kicker">ACCOUNT</span>
              <h2>账户持仓录入</h2>
            </div>
            <span>保存后首页自动更新</span>
          </div>
          <div className="form-row">
            <label>
              快照日期
              <input type="date" max={today} value={snapshotDate} onChange={(event) => setSnapshotDate(event.target.value)} />
            </label>
            <label>
              总资产
              <input type="text" value={formatMoney(totalAssetsValue)} readOnly />
            </label>
            <label>
              现金
              <input type="number" min="0" step="0.01" value={cash} onChange={(event) => setCash(event.target.value)} />
            </label>
          </div>
          <div className="table-wrap">
            <table className="market-table editor-table">
              <thead>
                <tr>
                  <th>代码</th>
                  <th>数量</th>
                  <th>成本价</th>
                  <th>名称</th>
                  <th>最新价</th>
                  <th>市值</th>
                  <th>盈亏</th>
                  <th>仓位</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {previewRows.map((row) => (
                  <tr key={row.key}>
                    <td>
                      <input value={row.symbol} onChange={(event) => updatePositionRow(row.key, "symbol", event.target.value)} />
                    </td>
                    <td>
                      <input type="number" min="0" step="1" value={row.quantity || ""} onChange={(event) => updatePositionRow(row.key, "quantity", event.target.value)} />
                    </td>
                    <td>
                      <input type="number" min="0" step="0.01" value={row.costPrice || ""} onChange={(event) => updatePositionRow(row.key, "costPrice", event.target.value)} />
                    </td>
                    <td>{row.name ?? "--"}</td>
                    <td>{row.symbol ? formatMoney(row.latestPrice) : "--"}</td>
                    <td>{row.symbol ? formatMoney(row.marketValue) : "--"}</td>
                    <td className={row.profitLoss >= 0 ? "up" : "down"}>{row.symbol ? formatMoney(row.profitLoss) : "--"}</td>
                    <td>{row.symbol ? formatPercent(row.positionPct) : "--"}</td>
                    <td>
                      <button className="table-button" type="button" onClick={() => removePositionRow(row.key)}>
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="form-actions">
            <button type="button" className="secondary-button" onClick={() => setPositionRows((rows) => [...rows, newPositionRow()])}>
              添加持仓
            </button>
            <button type="submit" className="trade-button buy">
              保存持仓快照
            </button>
          </div>
        </form>

        <section className="account-grid">
          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">SNAPSHOTS</span>
                <h2>账户快照</h2>
              </div>
              <span>{snapshots.length} 条</span>
            </div>
            <div className="timeline-list">
              {snapshots.map((item) => (
                <button key={item.id} type="button" onClick={() => setSnapshot(item)}>
                  <strong>{item.snapshot_date}</strong>
                  <span>总资产 {formatMoney(item.total_assets)}</span>
                  <small>{item.positions.length} 个持仓</small>
                </button>
              ))}
            </div>
          </section>

          <section className="terminal-panel">
            <div className="panel-title">
              <div>
                <span className="section-kicker">CHANGES</span>
                <h2>近期持仓变化</h2>
              </div>
              <span>{changes.length} 条</span>
            </div>
            <div className="table-wrap">
              <table className="market-table">
                <thead>
                  <tr>
                    <th>代码</th>
                    <th>名称</th>
                    <th>变化</th>
                    <th>前次</th>
                    <th>当前</th>
                    <th>差额</th>
                  </tr>
                </thead>
                <tbody>
                  {changes.map((change) => (
                    <tr key={`${change.symbol}-${change.change_type}`}>
                      <td className="symbol-cell">{change.symbol}</td>
                      <td>{change.name ?? "--"}</td>
                      <td>{change.change_type}</td>
                      <td>{change.previous_quantity}</td>
                      <td>{change.current_quantity}</td>
                      <td className={change.quantity_delta >= 0 ? "up" : "down"}>{change.quantity_delta}</td>
                    </tr>
                  ))}
                  {!changes.length && (
                    <tr>
                      <td colSpan={6}>
                        <div className="empty-state">至少保存两次快照后会显示持仓变化。</div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </section>
      </>
    );
  }

  return (
    <main className="trading-shell">
      <aside className="side-nav">
        <div className="brand-block">
          <span>A-SHARE</span>
          <strong>策略交易台</strong>
        </div>
        <button className={activeModule === "dashboard" ? "active" : ""} onClick={() => setActiveModule("dashboard")}>
          首页看板
        </button>
        <button className={activeModule === "strategy" ? "active" : ""} onClick={() => setActiveModule("strategy")}>
          策略模块
        </button>
        <button className={activeModule === "account" ? "active" : ""} onClick={() => setActiveModule("account")}>
          账户持仓
        </button>
      </aside>

      <section className="workspace">
        <header className="trade-topbar">
          <div>
            <span className="section-kicker">MARKET MONITOR</span>
            <h1>{activeModule === "dashboard" ? "首页数据看板" : activeModule === "strategy" ? "策略结果中心" : "账户持仓管理"}</h1>
          </div>
          <div className="user-box">
            <span>{user.username}</span>
            <button className="ghost-button" onClick={onLogout}>
              退出
            </button>
          </div>
        </header>

        {error && <div className="notice">{error}</div>}
        {message && <div className="notice success">{message}</div>}
        {scanResult && <div className="notice info">最近扫描：{scanResult.scan_date}，导入 {scanResult.imported} 条，来源 {scanResult.source_file}</div>}

        {activeModule === "dashboard" && renderDashboard()}
        {activeModule === "strategy" && renderStrategy()}
        {activeModule === "account" && renderAccount()}
      </section>

      {timerModal && (
        <div className="modal-backdrop" role="presentation">
          <form className="timer-modal" onSubmit={submitTimer}>
            <div className="panel-title">
              <div>
                <span className="section-kicker">TIMER</span>
                <h2>{timerModal === "quotes" ? "持仓行情刷新" : "策略实时扫描"}</h2>
              </div>
            </div>
            <label>
              间隔时间，单位秒
              <input
                type="number"
                min="5"
                step="1"
                value={intervalSeconds}
                onChange={(event) => setIntervalSeconds(event.target.value)}
                autoFocus
              />
            </label>
            <div className="form-actions">
              <button type="button" className="secondary-button" onClick={() => setTimerModal(null)}>
                取消
              </button>
              <button type="submit" className="trade-button buy">
                开启
              </button>
            </div>
          </form>
        </div>
      )}
    </main>
  );
}
