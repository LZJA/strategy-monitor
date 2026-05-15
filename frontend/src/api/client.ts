export type User = {
  id: number;
  username: string;
  email: string | null;
  role: string;
  status: string;
};

export type Signal = {
  id: number;
  signal_date: string;
  strategy_name: string;
  signal_type: string;
  symbol: string;
  name: string | null;
  close_price: number | null;
  high_price: number | null;
  breakout_price: number | null;
  stop_loss_price: number | null;
  take_profit_price: number | null;
  amount_rank: number | null;
  payload?: Record<string, unknown> | null;
};

export type Kline = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
  amount: number | null;
};

export type PatternPoint = {
  label: "A" | "B" | "C" | "D" | string;
  date: string;
  price: number;
};

export type SignalChart = {
  signal: Signal;
  klines: Kline[];
  points: PatternPoint[];
};

export type Position = {
  id: number;
  symbol: string;
  name: string | null;
  quantity: number;
  cost_price: number;
  latest_price: number | null;
  market_value: number | null;
  profit_loss: number | null;
  profit_loss_pct: number | null;
  position_pct: number | null;
  recent_signal_count: number;
};

export type PositionQuote = {
  symbol: string;
  name: string | null;
  latest_price: number | null;
};

export type Snapshot = {
  id: number;
  snapshot_date: string;
  total_assets: number;
  cash: number;
  note: string | null;
  positions: Position[];
};

export type ScanResult = {
  scan_run_id: number;
  scan_date: string;
  source_file: string;
  imported: number;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {})
    },
    ...init
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(detail.detail || "请求失败");
  }
  return response.json() as Promise<T>;
}

export const api = {
  bootstrap: () => request<{ has_users: boolean; registration_enabled: boolean }>("/api/auth/bootstrap"),
  me: () => request<User>("/api/auth/me"),
  login: (username: string, password: string) =>
    request<User>("/api/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  register: (username: string, password: string, email?: string) =>
    request<User>("/api/auth/register", { method: "POST", body: JSON.stringify({ username, password, email }) }),
  logout: () => request<{ ok: boolean }>("/api/auth/logout", { method: "POST" }),
  todaySignals: () => request<Signal[]>("/api/signals/today"),
  signalChart: (signalId: number) => request<SignalChart>(`/api/signals/${signalId}/chart`),
  scanToday: () => request<ScanResult>("/api/admin/scan/today", { method: "POST" }),
  currentSnapshot: () => request<Snapshot | null>("/api/account/current"),
  positionQuotes: (symbols: string[], snapshotDate: string) => {
    const params = new URLSearchParams({
      symbols: symbols.join(","),
      snapshot_date: snapshotDate
    });
    return request<PositionQuote[]>(`/api/account/position-quotes?${params.toString()}`);
  },
  saveSnapshot: (snapshot: unknown) =>
    request<Snapshot>("/api/account/snapshots", { method: "POST", body: JSON.stringify(snapshot) })
};
