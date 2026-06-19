import { render, screen, waitFor } from '@testing-library/react';
import type React from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { decisionSignalsApi } from '../../../api/decisionSignals';
import { UiLanguageProvider } from '../../../contexts/UiLanguageContext';
import type { DecisionSignalItem } from '../../../types/decisionSignals';
import { ReportDecisionSignals } from '../ReportDecisionSignals';

vi.mock('../../../api/decisionSignals', () => ({
  decisionSignalsApi: {
    list: vi.fn(),
  },
}));

const signal: DecisionSignalItem = {
  id: 21,
  stockCode: 'HK00700',
  stockName: '腾讯控股',
  market: 'hk',
  sourceType: 'analysis',
  sourceReportId: 5,
  marketPhase: 'postmarket',
  triggerSource: 'history',
  action: 'watch',
  actionLabel: null,
  confidence: 0.6,
  score: 70,
  horizon: '5d',
  entryLow: null,
  entryHigh: null,
  stopLoss: null,
  targetPrice: null,
  invalidation: null,
  watchConditions: '观察回购强度',
  reason: '结构等待确认',
  riskSummary: '缺少成交量确认',
  catalystSummary: null,
  evidence: undefined,
  dataQualitySummary: undefined,
  planQuality: 'partial',
  status: 'active',
  expiresAt: null,
  createdAt: '2026-06-17T08:00:00',
  updatedAt: '2026-06-17T08:00:00',
  metadata: undefined,
};

function renderComponent(props: React.ComponentProps<typeof ReportDecisionSignals>) {
  return render(
    <UiLanguageProvider>
      <ReportDecisionSignals {...props} />
    </UiLanguageProvider>,
  );
}

function deferredPromise<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  window.localStorage.clear();
  window.localStorage.setItem('dsa.uiLanguage', 'zh');
  vi.clearAllMocks();
  vi.mocked(decisionSignalsApi.list).mockResolvedValue({
    items: [signal],
    total: 1,
    page: 1,
    pageSize: 20,
  });
});

describe('ReportDecisionSignals', () => {
  it('loads and renders analysis-bound signals for a report record', async () => {
    renderComponent({ recordId: 5, reportType: 'detailed' });

    expect(await screen.findByText('腾讯控股')).toBeInTheDocument();
    expect(screen.getByText('缺少成交量确认')).toBeInTheDocument();
    expect(decisionSignalsApi.list).toHaveBeenCalledWith({
      sourceReportId: 5,
      sourceType: 'analysis',
      page: 1,
      pageSize: 20,
    });
  });

  it('shows an empty state when the report has no extracted signals', async () => {
    vi.mocked(decisionSignalsApi.list).mockResolvedValueOnce({
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
    });

    renderComponent({ recordId: 5, reportType: 'detailed' });

    expect(await screen.findByText('本报告暂无决策信号')).toBeInTheDocument();
  });

  it('clears previous report signals while loading another report', async () => {
    const secondLoad = deferredPromise<{
      items: DecisionSignalItem[];
      total: number;
      page: number;
      pageSize: number;
    }>();
    vi.mocked(decisionSignalsApi.list)
      .mockResolvedValueOnce({
        items: [signal],
        total: 1,
        page: 1,
        pageSize: 20,
      })
      .mockReturnValueOnce(secondLoad.promise);

    const { rerender } = render(
      <UiLanguageProvider>
        <ReportDecisionSignals recordId={5} reportType="detailed" />
      </UiLanguageProvider>,
    );

    expect(await screen.findByText('腾讯控股')).toBeInTheDocument();

    rerender(
      <UiLanguageProvider>
        <ReportDecisionSignals recordId={6} reportType="detailed" />
      </UiLanguageProvider>,
    );

    await waitFor(() => {
      expect(decisionSignalsApi.list).toHaveBeenLastCalledWith({
        sourceReportId: 6,
        sourceType: 'analysis',
        page: 1,
        pageSize: 20,
      });
    });
    expect(screen.queryByText('腾讯控股')).not.toBeInTheDocument();

    secondLoad.resolve({
      items: [],
      total: 0,
      page: 1,
      pageSize: 20,
    });
    expect(await screen.findByText('本报告暂无决策信号')).toBeInTheDocument();
  });

  it('shows an error state when loading report signals fails', async () => {
    vi.mocked(decisionSignalsApi.list).mockRejectedValueOnce(new Error('network down'));

    renderComponent({ recordId: 5, reportType: 'detailed' });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('报告信号加载失败');
    expect(alert).toHaveTextContent('network down');
  });

  it('does not render or request without a record id', () => {
    const { container } = renderComponent({ reportType: 'detailed' });

    expect(container).toBeEmptyDOMElement();
    expect(decisionSignalsApi.list).not.toHaveBeenCalled();
  });

  it('does not render or request for market review reports', async () => {
    const { container } = renderComponent({ recordId: 5, reportType: 'market_review' });

    await waitFor(() => expect(decisionSignalsApi.list).not.toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
