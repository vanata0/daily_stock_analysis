import type React from 'react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity } from 'lucide-react';
import { decisionSignalsApi } from '../../api/decisionSignals';
import { getParsedApiError, type ParsedApiError } from '../../api/error';
import { ApiErrorAlert, Card, EmptyState } from '../common';
import { DecisionSignalCard } from '../decision-signals/DecisionSignalDisplay';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { ReportType } from '../../types/analysis';
import type { DecisionSignalItem } from '../../types/decisionSignals';

interface ReportDecisionSignalsProps {
  recordId?: number;
  reportType?: ReportType;
}

export const ReportDecisionSignals: React.FC<ReportDecisionSignalsProps> = ({
  recordId,
  reportType,
}) => {
  const { t } = useUiLanguage();
  const [items, setItems] = useState<DecisionSignalItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const requestIdRef = useRef(0);
  const shouldRender = Boolean(recordId) && reportType !== 'market_review';

  const loadSignals = useCallback(async () => {
    if (!recordId || reportType === 'market_review') return;
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    setItems([]);
    setError(null);
    try {
      const response = await decisionSignalsApi.list({
        sourceReportId: recordId,
        sourceType: 'analysis',
        page: 1,
        pageSize: 20,
      });
      if (requestIdRef.current !== requestId) return;
      setItems(response.items);
      setError(null);
    } catch (err) {
      if (requestIdRef.current !== requestId) return;
      setError(getParsedApiError(err));
      setItems([]);
    } finally {
      if (requestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [recordId, reportType]);

  useEffect(() => {
    if (!shouldRender) {
      requestIdRef.current += 1;
      setLoading(false);
      setItems([]);
      setError(null);
      return;
    }
    void loadSignals();
    return () => {
      requestIdRef.current += 1;
    };
  }, [loadSignals, shouldRender]);

  if (!shouldRender) {
    return null;
  }

  return (
    <Card
      title={t('decisionSignals.reportSectionTitle')}
      subtitle={t('decisionSignals.reportSectionDescription')}
      padding="md"
    >
      {error ? (
        <ApiErrorAlert
          error={{ ...error, title: t('decisionSignals.reportErrorTitle') }}
          actionLabel={t('common.retry')}
          onAction={() => void loadSignals()}
        />
      ) : null}
      {loading && items.length === 0 ? (
        <div className="grid gap-3">
          <div className="h-24 animate-pulse rounded-2xl border border-border/70 bg-card/60" />
          <div className="h-24 animate-pulse rounded-2xl border border-border/70 bg-card/60" />
        </div>
      ) : null}
      {!loading && !error && items.length === 0 ? (
        <EmptyState
          className="border-none bg-transparent py-6 shadow-none"
          title={t('decisionSignals.reportEmptyTitle')}
          description={t('decisionSignals.reportEmptyDescription')}
          icon={<Activity className="h-6 w-6" />}
        />
      ) : null}
      {items.length > 0 ? (
        <div className="grid gap-3 lg:grid-cols-2">
          {items.map((item) => (
            <DecisionSignalCard key={item.id} item={item} />
          ))}
        </div>
      ) : null}
    </Card>
  );
};
