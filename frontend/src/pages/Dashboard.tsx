import { FormEvent, useEffect, useMemo, useState } from "react";
import { api, PositionQuote, ScanResult, Signal, SignalChart, Snapshot, User } from "../api/client";
import { SignalKlineChart } from "./SignalKlineChart";

type Props = {
  user: User;
  onLogout: () => void;
};

function localDateString(value = new Date()) {
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

const today = localDateString();
const money = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const percent = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2, style: "percent" });

type PositionRow = {
  key: string;
  symbol: string;
  quantity: string;
  costPrice: string;
  name?: string | null;
  latestPrice?: number | null;
};

function formatMoney(value: number | null | undefined) {
  return value == null ? "--" : money.format(value);
}

function formatPercent(value: number | null | undefined) {
  return value == null || Number.isNaN(value) ? "--" : percent.format(value);
}

function newPositionRow(): PositionRow {
  return {
    key: crypto.randomUUID(),
    symbol: "",
    quantity: "",
    costPrice: ""
  };
}

function toNumber(value: string) {
  return value === "" ? 0 : Number(value);
}

export function Dashboard({ user, onLogout }: Props) {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [scanning, setScanning] = useState(false);
  const [scanResult, setScanResult] = useState<ScanResult | null>(null);
  const [selectedSignalId, setSelectedSignalId] = useState<number | null>(null);
  const [signalChart, setSignalChart] = useState<SignalChart | null>(null);
  const [chartLoading, setChartLoading] = useState(false);
  const [snapshotDate, setSnapshotDate] = useState(today);
  const [totalAssets, setTotalAssets] = useState("");
  const [cash, setCash] = useState("");
  const [positionRows, setPositionRows] = useState<PositionRow[]>([
    { ...newPositionRow(), symbol: "600000", quantity: "100", costPrice: "10.50" }
  ]);
  const [quotes, setQuotes] = useState<Record<string, PositionQuote>>({});
  const isAdmin = user.role === "admin";

  const grouped = useMemo(() => {
    return signals.reduce<Record<string, number>>((acc, signal) => {
      acc[signal.strategy_name] = (acc[signal.strategy_name] ?? 0) + 1;
      return acc;
    }, {});
  }, [signals]);

  const positionMarketValue = snapshot?.positions.reduce((sum, position) => sum + (position.market_value ?? 0), 0) ?? 0;
  const positionProfitLoss = snapshot?.positions.reduce((sum, position) => sum + (position.profit_loss ?? 0), 0) ?? 0;
  const latestSignalDate = signals[0]?.signal_date ?? "--";
  const selectedSignal = signals.find((signal) => signal.id === selectedSignalId) ?? signals[0] ?? null;
  const totalAssetsValue = toNumber(totalAssets);
  const previewRows = positionRows.map((row) => {
    const symbol = row.symbol.trim().toUpperCase();
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
      profitLossPct: costValue ? profitLoss / costValue : 0,
      positionPct: totalAssetsValue ? marketValue / totalAssetsValue : 0
    };
  });

  async function refresh() {
    const [nextSignals, nextSnapshot] = await Promise.all([api.todaySignals(), api.currentSnapshot()]);
    setSignals(nextSignals);
    setSnapshot(nextSnapshot);
    if (nextSnapshot) {
      setSnapshotDate(nextSnapshot.snapshot_date);
      setTotalAssets(String(nextSnapshot.total_assets));
      setCash(String(nextSnapshot.cash));
      setPositionRows(
        nextSnapshot.positions.length
          ? nextSnapshot.positions.map((p) => ({
              key: crypto.randomUUID(),
              symbol: p.symbol,
              quantity: String(p.quantity),
              costPrice: String(p.cost_price),
              name: p.name,
              latestPrice: p.latest_price
            }))
          : [newPositionRow()]
      );
    }
  }

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof Error ? err.message : "加载失败"));
  }, []);

  useEffect(() => {
    if (!signals.length) {
      setSelectedSignalId(null);
      setSignalChart(null);
      return;
    }
    if (!selectedSignalId || !signals.some((signal) => signal.id === selectedSignalId)) {
      setSelectedSignalId(signals[0].id);
    }
  }, [signals, selectedSignalId]);

  useEffect(() => {
    if (!selectedSignalId) {
      return;
    }
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
    const symbols = Array.from(
      new Set(positionRows.map((row) => row.symbol.trim().toUpperCase()).filter(Boolean))
    );
    if (!symbols.length || !snapshotDate) {
      setQuotes({});
      return;
    }
    const timer = window.setTimeout(() => {
      api
        .positionQuotes(symbols, snapshotDate)
        .then((items) => {
          setQuotes(
            items.reduce<Record<string, PositionQuote>>((acc, item) => {
              acc[item.symbol] = item;
              return acc;
            }, {})
          );
        })
        .catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [positionRows, snapshotDate]);

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
        total_assets: Number(totalAssets),
        cash: Number(cash),
        positions
      });
      await refresh();
      setMessage("账户快照已保存");
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    }
  }

  async function scanToday() {
    setError("");
    setMessage("");
    setScanning(true);
    try {
      const result = await api.scanToday();
      setScanResult(result);
      await refresh();
      setMessage(`已导入 ${result.imported} 条今日信号`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "扫描失败");
    } finally {
      setScanning(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">A 股策略信号</p>
          <h1>策略监控台</h1>
        </div>
        <div className="user-box">
          <span>{user.username}</span>
          <button className="ghost-button" onClick={onLogout}>退出</button>
        </div>
      </header>

      {error && <div className="notice">{error}</div>}
      {message && <div className="notice success">{message}</div>}

      <section className="hero-band">
        <div>
          <p className="eyebrow">今日交易视图</p>
          <h2>扫描、看形态、再决定要不要动手。</h2>
          <p className="muted">
            点击扫描会调用形态工具并导入命中结果；选择标的后可直接查看 K 线结构和 ABCD 点位。
          </p>
        </div>
        <div className="hero-actions">
          <button className="primary-action" onClick={scanToday} disabled={!isAdmin || scanning}>
            {scanning ? "扫描中..." : "扫描今日信号"}
          </button>
          {!isAdmin && <span className="hint">仅管理员可扫描</span>}
          {scanResult && (
            <span className="hint">
              最近：{scanResult.imported} 条，{scanResult.source_file}
            </span>
          )}
        </div>
      </section>

      <section className="metrics-grid">
        <article>
          <span>今日信号</span>
          <strong>{signals.length}</strong>
          <small>信号日期 {latestSignalDate}</small>
        </article>
        <article>
          <span>当前总资产</span>
          <strong>{formatMoney(snapshot?.total_assets)}</strong>
          <small>快照 {snapshot?.snapshot_date ?? "--"}</small>
        </article>
        <article>
          <span>持仓市值</span>
          <strong>{formatMoney(positionMarketValue)}</strong>
          <small>现金 {formatMoney(snapshot?.cash)}</small>
        </article>
        <article>
          <span>浮盈亏</span>
          <strong className={positionProfitLoss >= 0 ? "up" : "down"}>{formatMoney(positionProfitLoss)}</strong>
          <small>{snapshot?.positions.length ?? 0} 个持仓</small>
        </article>
      </section>

      <section className="content-grid">
        <div className="panel">
          <div className="panel-title">
            <h2>今日策略分布</h2>
            <span>{signals.length} 条</span>
          </div>
          <div className="strategy-list">
            {Object.entries(grouped).map(([name, count]) => (
              <div key={name}>
                <span>{name}</span>
                <strong>{count}</strong>
              </div>
            ))}
            {!signals.length && (
              <div className="empty-state">
                <strong>还没有今日信号</strong>
                <span>点击右上方“扫描今日信号”，或把扫描 CSV 放入 data/scan_results 后再试。</span>
              </div>
            )}
          </div>
        </div>

        <form className="panel" onSubmit={saveSnapshot}>
          <div className="panel-title">
            <h2>账户快照</h2>
            <span>持仓录入</span>
          </div>
          <div className="form-row">
            <label>
              快照日期
              <input
                type="date"
                max={today}
                value={snapshotDate}
                onChange={(event) => setSnapshotDate(event.target.value)}
              />
            </label>
            <label>
              总资产
              <input
                type="number"
                min="0"
                step="0.01"
                value={totalAssets}
                onChange={(event) => setTotalAssets(event.target.value)}
              />
            </label>
            <label>
              现金
              <input
                type="number"
                min="0"
                step="0.01"
                value={cash}
                onChange={(event) => setCash(event.target.value)}
              />
            </label>
          </div>
          <div className="position-editor">
            <table>
              <thead>
                <tr>
                  <th>股票代码</th>
                  <th>数量</th>
                  <th>成本价</th>
                  <th>股票名称</th>
                  <th>最新价</th>
                  <th>持仓市值</th>
                  <th>浮盈亏</th>
                  <th>浮盈亏比例</th>
                  <th>仓位比例</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {previewRows.map((row) => (
                  <tr key={row.key}>
                    <td>
                      <input
                        className="compact-input"
                        value={row.symbol}
                        onChange={(event) => updatePositionRow(row.key, "symbol", event.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        className="compact-input"
                        type="number"
                        min="0"
                        step="1"
                        value={row.quantity || ""}
                        onChange={(event) => updatePositionRow(row.key, "quantity", event.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        className="compact-input"
                        type="number"
                        min="0"
                        step="0.01"
                        value={row.costPrice || ""}
                        onChange={(event) => updatePositionRow(row.key, "costPrice", event.target.value)}
                      />
                    </td>
                    <td>{row.name ?? "--"}</td>
                    <td>{row.symbol ? formatMoney(row.latestPrice) : "--"}</td>
                    <td>{row.symbol ? formatMoney(row.marketValue) : "--"}</td>
                    <td className={row.profitLoss >= 0 ? "up" : "down"}>
                      {row.symbol ? formatMoney(row.profitLoss) : "--"}
                    </td>
                    <td className={row.profitLoss >= 0 ? "up" : "down"}>
                      {row.symbol ? formatPercent(row.profitLossPct) : "--"}
                    </td>
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
            <button type="submit">保存快照</button>
          </div>
        </form>
      </section>

      <section className="signal-workspace">
        <div className="panel signal-list-panel">
          <div className="panel-title">
            <h2>命中标的</h2>
            <span>{signals.length} 条</span>
          </div>
          <div className="signal-list">
            {signals.map((signal) => (
              <button
                className={`signal-card ${selectedSignal?.id === signal.id ? "active" : ""}`}
                key={signal.id}
                type="button"
                onClick={() => setSelectedSignalId(signal.id)}
              >
                <span>
                  <strong>{signal.symbol}</strong>
                  {signal.name ?? "--"}
                </span>
                <em>{signal.strategy_name}</em>
                <small>
                  收盘 {signal.close_price ?? "--"} / 突破 {signal.breakout_price ?? "--"}
                </small>
              </button>
            ))}
            {!signals.length && (
              <div className="empty-state">
                <strong>还没有命中标的</strong>
                <span>先点击“扫描今日信号”，扫描完成后这里会出现可点击的标的列表。</span>
              </div>
            )}
          </div>
        </div>

        <div className="panel chart-panel">
          <div className="panel-title">
            <h2>{selectedSignal ? `${selectedSignal.symbol} ${selectedSignal.name ?? ""}` : "K 线结构"}</h2>
            <span>{selectedSignal?.strategy_name ?? "ABCD"}</span>
          </div>
          {selectedSignal && (
            <div className="signal-summary">
              <span>日期 {selectedSignal.signal_date}</span>
              <span>收盘 {selectedSignal.close_price ?? "--"}</span>
              <span>最高 {selectedSignal.high_price ?? "--"}</span>
              <span>突破 {selectedSignal.breakout_price ?? "--"}</span>
              <span>止损 {selectedSignal.stop_loss_price ?? "--"}</span>
              <span>止盈 {selectedSignal.take_profit_price ?? "--"}</span>
            </div>
          )}
          <SignalKlineChart chart={signalChart} loading={chartLoading} />
          {!!signalChart?.points.length && (
            <div className="point-strip">
              {signalChart.points.map((point) => (
                <span key={point.label}>
                  <strong>{point.label}</strong>
                  {point.date} / {point.price.toFixed(2)}
                </span>
              ))}
            </div>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel-title">
          <h2>当前持仓</h2>
          <span>{snapshot?.positions.length ?? 0} 个标的</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>代码</th>
              <th>名称</th>
              <th>数量</th>
              <th>成本价</th>
              <th>最新价</th>
              <th>市值</th>
              <th>浮盈亏</th>
              <th>浮盈亏比例</th>
              <th>仓位比例</th>
              <th>近期信号</th>
            </tr>
          </thead>
          <tbody>
            {snapshot?.positions.map((position) => (
              <tr key={position.id}>
                <td>{position.symbol}</td>
                <td>{position.name ?? "--"}</td>
                <td>{position.quantity}</td>
                <td>{position.cost_price}</td>
                <td>{formatMoney(position.latest_price)}</td>
                <td>{formatMoney(position.market_value)}</td>
                <td className={(position.profit_loss ?? 0) >= 0 ? "up" : "down"}>{formatMoney(position.profit_loss)}</td>
                <td className={(position.profit_loss ?? 0) >= 0 ? "up" : "down"}>
                  {formatPercent(position.profit_loss_pct)}
                </td>
                <td>{formatPercent(position.position_pct)}</td>
                <td>{position.recent_signal_count ? `${position.recent_signal_count} 条` : "无"}</td>
              </tr>
            ))}
            {!snapshot?.positions.length && (
              <tr>
                <td colSpan={10}>
                  <div className="table-empty">暂无持仓，先保存一份账户快照。</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>

      <section className="panel compact-signals-panel">
        <div className="panel-title">
          <h2>今日信号明细</h2>
          <span>点击行可切换 K 线</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>日期</th>
              <th>策略</th>
              <th>类型</th>
              <th>代码</th>
              <th>名称</th>
              <th>收盘</th>
              <th>突破价</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((signal) => (
              <tr
                className={selectedSignal?.id === signal.id ? "selected-row" : ""}
                key={signal.id}
                onClick={() => setSelectedSignalId(signal.id)}
              >
                <td>{signal.signal_date}</td>
                <td>{signal.strategy_name}</td>
                <td>{signal.signal_type}</td>
                <td>{signal.symbol}</td>
                <td>{signal.name ?? "--"}</td>
                <td>{signal.close_price ?? "--"}</td>
                <td>{signal.breakout_price ?? "--"}</td>
              </tr>
            ))}
            {!signals.length && (
              <tr>
                <td colSpan={7}>
                  <div className="table-empty">今日信号为空。点击“扫描今日信号”后这里会显示入库结果。</div>
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </section>
    </main>
  );
}
