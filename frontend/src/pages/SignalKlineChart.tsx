import { useEffect, useRef } from "react";
import * as echarts from "echarts";
import { SignalChart } from "../api/client";

type Props = {
  chart: SignalChart | null;
  loading: boolean;
};

const pointColors: Record<string, string> = {
  A: "#2563eb",
  B: "#7c3aed",
  C: "#0891b2",
  D: "#f59e0b"
};

export function SignalKlineChart({ chart, loading }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current || !chart?.klines.length) {
      return;
    }
    const instance = echarts.init(ref.current);
    const dates = chart.klines.map((item) => item.date);
    const candles = chart.klines.map((item) => [item.open, item.close, item.low, item.high]);
    const volumes = chart.klines.map((item) => item.volume ?? 0);
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

    instance.setOption({
      animation: false,
      grid: [
        { left: 54, right: 24, top: 34, height: 260 },
        { left: 54, right: 24, top: 326, height: 72 }
      ],
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        borderWidth: 1,
        confine: true
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
          axisLabel: { color: "#8a97a6" },
          splitLine: { show: false }
        }
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1], start: 45, end: 100 },
        { type: "slider", xAxisIndex: [0, 1], bottom: 8, height: 18, start: 45, end: 100 }
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
            label: { color: "#334155", formatter: "突破价" },
            data: chart.signal.breakout_price
              ? [{ yAxis: chart.signal.breakout_price }]
              : []
          }
        },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volumes,
          itemStyle: { color: "#a7b3c2" }
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
