import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import { SignalChart } from "../api/client";

type Props = {
  chart: Pick<SignalChart, "klines" | "points"> & { signal?: SignalChart["signal"] | null } | null;
  loading: boolean;
  priceLines?: Array<{ name: string; value: number | null | undefined; color: string }>;
};

const pointColors: Record<string, string> = {
  A: "#2563eb",
  B: "#7c3aed",
  C: "#0891b2",
  D: "#f59e0b"
};

function formatPrice(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(2) : "--";
}

function formatPct(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? `${value.toFixed(2)}%` : "--";
}

function formatAmount(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "--";
  }
  if (Math.abs(value) >= 100000000) {
    return `${(value / 100000000).toFixed(2)}亿`;
  }
  if (Math.abs(value) >= 10000) {
    return `${(value / 10000).toFixed(2)}万`;
  }
  return value.toFixed(0);
}

export function SignalKlineChart({ chart, loading, priceLines = [] }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current || !chart?.klines.length) {
      return;
    }
    const instance = echarts.init(ref.current);
    const dates = chart.klines.map((item) => item.date);
    const candles = chart.klines.map((item) => [item.open, item.close, item.low, item.high]);
    const amounts = chart.klines.map((item) => item.amount ?? 0);
    const defaultVisibleBars = 88;
    const zoomStart = dates.length > defaultVisibleBars ? ((dates.length - defaultVisibleBars) / dates.length) * 100 : 0;
    const pointByLabel = new Map(chart.points.map((point) => [point.label, point]));
    const pointData = chart.points
      .map((point) => {
        const index = dates.indexOf(point.date);
        if (index < 0) {
          return null;
        }
        return {
          name: point.label,
          value: point.label,
          coord: [point.date, point.price],
          itemStyle: { color: pointColors[point.label] ?? "#111827" },
          label: {
            color: "#ffffff",
            fontWeight: 700,
            formatter: point.label
          }
        };
      })
      .filter(Boolean);
    const markLineData: unknown[] = chart.signal?.breakout_price
      ? [
          {
            name: "突破价",
            yAxis: chart.signal.breakout_price,
            lineStyle: { color: "#facc15", width: 2.4, type: "dashed" },
            label: {
              color: "#fef3c7",
              backgroundColor: "#111827",
              borderColor: "#facc15",
              borderWidth: 1,
              borderRadius: 4,
              padding: [3, 6],
              formatter: "突破价"
            }
          }
        ]
      : [];
    priceLines.forEach((line) => {
      if (typeof line.value === "number" && Number.isFinite(line.value)) {
        markLineData.push({
          name: line.name,
          yAxis: line.value,
          lineStyle: { color: line.color, width: 1.8, type: "dashed" },
          label: { color: line.color, formatter: `${line.name} ${formatPrice(line.value)}` }
        });
      }
    });
    const abLineData: Array<number | null> = dates.map(() => null);
    const abLabelData: unknown[] = [];
    const pointA = pointByLabel.get("A");
    const pointB = pointByLabel.get("B");
    if (pointA && pointB) {
      const indexA = dates.indexOf(pointA.date);
      const indexB = dates.indexOf(pointB.date);
      const latestIndex = dates.length - 1;
      if (indexA >= 0 && indexB > indexA && latestIndex > indexB) {
        const slope = (pointB.price - pointA.price) / (indexB - indexA);
        for (let index = indexA; index <= latestIndex; index += 1) {
          abLineData[index] = Number((pointA.price + slope * (index - indexA)).toFixed(4));
        }
        const labelIndex = Math.min(latestIndex, Math.max(indexB, Math.round((indexB + latestIndex) / 2)));
        abLabelData.push({
          coord: [dates[labelIndex], abLineData[labelIndex]],
          symbol: "circle",
          symbolSize: 1,
          itemStyle: { opacity: 0 },
          label: {
            show: true,
            color: "#fff7ed",
            backgroundColor: "#111827",
            borderColor: "#f97316",
            borderWidth: 1,
            borderRadius: 4,
            padding: [3, 6],
            formatter: "AB颈线"
          }
        });
      }
    }

    instance.setOption({
      animation: false,
      grid: [
        { left: 54, right: 78, top: 34, height: 260 },
        { left: 54, right: 78, top: 326, height: 72 }
      ],
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        borderWidth: 1,
        confine: true,
        formatter: (params: unknown) => {
          const items = Array.isArray(params) ? params : [params];
          const first = items[0] as { dataIndex?: number; axisValue?: string } | undefined;
          const index = first?.dataIndex ?? 0;
          const kline = chart.klines[index];
          if (!kline) {
            return "";
          }
          return [
            `<div style="font-weight:600;margin-bottom:6px;">${first?.axisValue ?? kline.date}</div>`,
            `<div>开盘价：<b>${formatPrice(kline.open)}</b></div>`,
            `<div>当前价：<b>${formatPrice(kline.close)}</b></div>`,
            `<div>最低价：<b>${formatPrice(kline.low)}</b></div>`,
            `<div>最高价：<b>${formatPrice(kline.high)}</b></div>`,
            `<div>涨幅：<b>${formatPct(kline.change_pct)}</b></div>`,
            `<div>成交额：<b>${formatAmount(kline.amount)}</b></div>`
          ].join("");
        }
      },
      axisPointer: {
        link: [{ xAxisIndex: "all" }]
      },
      xAxis: [
        {
          type: "category",
          data: dates,
          boundaryGap: false,
          axisLine: { lineStyle: { color: "#aab6c3" } },
          axisLabel: { color: "#617184" },
          min: "dataMin",
          max: "dataMax"
        },
        {
          type: "category",
          gridIndex: 1,
          data: dates,
          boundaryGap: false,
          axisLabel: { show: false },
          axisTick: { show: false },
          axisLine: { lineStyle: { color: "#d5dee8" } },
          min: "dataMin",
          max: "dataMax"
        }
      ],
      yAxis: [
        {
          scale: true,
          splitLine: { lineStyle: { color: "#edf2f7" } },
          axisLabel: { color: "#617184" }
        },
        {
          scale: true,
          gridIndex: 1,
          splitNumber: 2,
          axisLabel: { color: "#8a97a6", formatter: (value: number) => formatAmount(value) },
          splitLine: { show: false }
        }
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: zoomStart, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], bottom: 8, height: 18, start: zoomStart, end: 100 }
      ],
      series: [
        {
          name: "K线",
          type: "candlestick",
          data: candles,
          itemStyle: {
            color: "#d64545",
            color0: "#178f68",
            borderColor: "#d64545",
            borderColor0: "#178f68"
          },
          markPoint: {
            symbol: "pin",
            symbolSize: 44,
            data: pointData
          },
          markLine: {
            symbol: "none",
            lineStyle: { color: "#334155", width: 1.5, type: "dashed" },
            label: { color: "#334155", formatter: ({ name }: { name?: string }) => name ?? "" },
            data: markLineData
          }
        },
        {
          name: "成交额",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: amounts,
          itemStyle: { color: "#a7b3c2" }
        },
        {
          name: "AB颈线",
          type: "line",
          data: abLineData,
          showSymbol: false,
          connectNulls: false,
          silent: true,
          lineStyle: { color: "#f97316", width: 2.2 },
          emphasis: { disabled: true },
          markPoint: {
            silent: true,
            data: abLabelData
          }
        }
      ]
    });

    const resize = () => instance.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      instance.dispose();
    };
  }, [chart]);

  if (loading) {
    return <div className="chart-state">K 线加载中...</div>;
  }

  if (!chart?.klines.length) {
    return <div className="chart-state">暂无 K 线缓存，扫描或同步行情后再查看。</div>;
  }

  return <div className="kline-chart" ref={ref} />;
}
