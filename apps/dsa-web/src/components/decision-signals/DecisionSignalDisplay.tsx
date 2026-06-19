import type React from 'react';
import { Badge, Card, JsonViewer } from '../common';
import { useUiLanguage } from '../../contexts/UiLanguageContext';
import type { UiLanguage, UiTextKey } from '../../i18n/uiText';
import type {
  DecisionSignalFeedbackItem,
  DecisionSignalFeedbackValue,
  DecisionSignalItem,
  DecisionSignalOutcomeItem,
  DecisionSignalOutcomeValue,
  DecisionSignalStatus,
} from '../../types/decisionSignals';
import {
  buildDecisionActionLabelMap,
  getDecisionActionLabel,
  getDecisionActionTone,
  type DecisionActionTone,
} from '../../utils/decisionAction';
import { cn } from '../../utils/cn';
import { parseDecisionSignalDate } from '../../utils/decisionSignalTime';

type BadgeVariant = 'default' | 'success' | 'warning' | 'danger' | 'info' | 'history';

const STATUS_LABEL_KEYS: Record<DecisionSignalStatus, UiTextKey> = {
  active: 'decisionSignals.active',
  expired: 'decisionSignals.expired',
  invalidated: 'decisionSignals.invalidated',
  closed: 'decisionSignals.closed',
  archived: 'decisionSignals.archived',
};

const STATUS_VARIANTS: Record<DecisionSignalStatus, BadgeVariant> = {
  active: 'success',
  expired: 'warning',
  invalidated: 'danger',
  closed: 'default',
  archived: 'history',
};

const ACTION_VARIANTS: Record<DecisionActionTone, BadgeVariant> = {
  success: 'success',
  warning: 'warning',
  danger: 'danger',
  default: 'default',
};

const OUTCOME_VARIANTS: Record<DecisionSignalOutcomeValue, BadgeVariant> = {
  hit: 'success',
  miss: 'danger',
  neutral: 'warning',
};

const LOCALE_BY_LANGUAGE: Record<UiLanguage, string> = {
  zh: 'zh-CN',
  en: 'en-US',
};

function formatDateTime(value: string | null | undefined, language: UiLanguage): string {
  const date = parseDecisionSignalDate(value);
  if (!date) return '-';
  return new Intl.DateTimeFormat(LOCALE_BY_LANGUAGE[language], {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  return Number(value).toFixed(2).replace(/\.?0+$/, '');
}

function formatPercent(value: number | null | undefined): string {
  const number = formatNumber(value);
  return number === '-' ? number : `${number}%`;
}

function formatEntryRange(item: DecisionSignalItem): string {
  const hasLow = item.entryLow !== null && item.entryLow !== undefined;
  const hasHigh = item.entryHigh !== null && item.entryHigh !== undefined;
  if (hasLow && hasHigh) {
    return item.entryLow === item.entryHigh
      ? formatNumber(item.entryLow)
      : `${formatNumber(item.entryLow)} - ${formatNumber(item.entryHigh)}`;
  }
  if (hasLow) return formatNumber(item.entryLow);
  if (hasHigh) return formatNumber(item.entryHigh);
  return '-';
}

function formatJsonish(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value.trim() || null;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function asJsonViewerData(value: unknown): Record<string, unknown> | unknown[] | null {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') return value as Record<string, unknown>;
  return null;
}

function getActionLabel(item: DecisionSignalItem, t: (key: UiTextKey) => string): string {
  return getDecisionActionLabel(
    item.action,
    item.actionLabel,
    null,
    t('decisionSignals.action'),
    buildDecisionActionLabelMap(t),
  ) ?? t('decisionSignals.action');
}

function getActionVariant(item: DecisionSignalItem): BadgeVariant {
  return ACTION_VARIANTS[getDecisionActionTone(item.action, item.actionLabel, null)];
}

function getMarketLabel(market: DecisionSignalItem['market'], t: (key: UiTextKey) => string): string {
  const key = `decisionSignals.market.${market}` as UiTextKey;
  return t(key);
}

function getOutcomeLabel(value: DecisionSignalOutcomeValue | null | undefined, t: (key: UiTextKey) => string): string {
  if (!value) return '-';
  const key = `decisionSignals.outcome.${value}` as UiTextKey;
  return t(key);
}

function getFeedbackLabel(value: DecisionSignalFeedbackValue | null | undefined, t: (key: UiTextKey) => string): string {
  if (!value) return t('decisionSignals.feedbackNone');
  const key = `decisionSignals.feedback.${value}` as UiTextKey;
  return t(key);
}

type DecisionSignalCardProps = {
  item: DecisionSignalItem;
  onSelect?: (item: DecisionSignalItem) => void;
  selected?: boolean;
};

export const DecisionSignalCard: React.FC<DecisionSignalCardProps> = ({ item, onSelect, selected = false }) => {
  const { language, t } = useUiLanguage();
  const actionLabel = getActionLabel(item, t);
  const interactive = Boolean(onSelect);
  const className = cn(
    'block w-full rounded-2xl border bg-card/70 p-4 text-left',
    interactive ? 'transition-colors hover:border-cyan/40 hover:bg-hover/70' : '',
    selected ? 'border-cyan/50 bg-cyan/10' : 'border-border/70',
  );
  const content = (
    <>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={getActionVariant(item)}>{actionLabel}</Badge>
            <Badge variant={STATUS_VARIANTS[item.status]}>{t(STATUS_LABEL_KEYS[item.status])}</Badge>
            <span className="font-mono text-sm text-secondary-text">{item.stockCode}</span>
          </div>
          <h3 className="mt-2 text-base font-semibold text-foreground">
            {item.stockName || item.stockCode}
          </h3>
        </div>
        <div className="text-right text-xs text-secondary-text">
          <div>{getMarketLabel(item.market, t)}</div>
          <div className="mt-1">{formatDateTime(item.createdAt, language)}</div>
        </div>
      </div>
      <div className="mt-3 grid gap-2 text-sm text-secondary-text">
        {item.reason ? <p className="line-clamp-2">{item.reason}</p> : null}
        {item.riskSummary ? <p className="line-clamp-2 text-warning">{item.riskSummary}</p> : null}
        {item.watchConditions ? <p className="line-clamp-2">{item.watchConditions}</p> : null}
      </div>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-text">
        <span>{t('decisionSignals.horizon')}: {item.horizon || '-'}</span>
        <span>{t('decisionSignals.planQuality')}: {item.planQuality}</span>
        <span>{t('decisionSignals.marketPhase')}: {item.marketPhase || '-'}</span>
      </div>
    </>
  );

  if (!interactive) {
    return <div className={className}>{content}</div>;
  }

  return (
    <div className={className}>
      {content}
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={() => onSelect?.(item)}
          className="btn-secondary !px-3 !py-1.5 !text-xs"
          aria-label={t('decisionSignals.viewDetailsFor', { stock: item.stockName || item.stockCode })}
        >
          {t('common.details')}
        </button>
      </div>
    </div>
  );
};

type DetailRowProps = {
  label: string;
  value?: React.ReactNode;
};

const DetailRow: React.FC<DetailRowProps> = ({ label, value }) => (
  <div className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-2">
    <p className="text-xs text-secondary-text">{label}</p>
    <div className="mt-1 text-sm text-foreground">{value || '-'}</div>
  </div>
);

type DecisionSignalDetailsProps = {
  item: DecisionSignalItem;
  actions?: React.ReactNode;
  outcomes?: DecisionSignalOutcomeItem[];
  outcomesLoading?: boolean;
  outcomesError?: string | null;
  feedback?: DecisionSignalFeedbackItem | null;
  feedbackLoading?: boolean;
  feedbackSaving?: boolean;
  feedbackError?: string | null;
  onFeedbackSubmit?: (value: DecisionSignalFeedbackValue) => void;
};

export const DecisionSignalDetails: React.FC<DecisionSignalDetailsProps> = ({
  item,
  actions,
  outcomes = [],
  outcomesLoading = false,
  outcomesError = null,
  feedback = null,
  feedbackLoading = false,
  feedbackSaving = false,
  feedbackError = null,
  onFeedbackSubmit,
}) => {
  const { language, t } = useUiLanguage();
  const actionLabel = getActionLabel(item, t);
  const entryRange = formatEntryRange(item);
  const evidenceData = asJsonViewerData(item.evidence);
  const qualityData = asJsonViewerData(item.dataQualitySummary);
  const metadataData = asJsonViewerData(item.metadata);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={getActionVariant(item)} size="md">{actionLabel}</Badge>
            <Badge variant={STATUS_VARIANTS[item.status]} size="md">{t(STATUS_LABEL_KEYS[item.status])}</Badge>
          </div>
          <h3 className="mt-3 text-xl font-semibold text-foreground">{item.stockName || item.stockCode}</h3>
          <p className="mt-1 font-mono text-sm text-secondary-text">{item.stockCode} · {getMarketLabel(item.market, t)}</p>
        </div>
        {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <DetailRow label={t('decisionSignals.score')} value={formatNumber(item.score)} />
        <DetailRow label={t('decisionSignals.confidence')} value={formatNumber(item.confidence)} />
        <DetailRow label={t('decisionSignals.horizon')} value={item.horizon || '-'} />
        <DetailRow label={t('decisionSignals.planQuality')} value={item.planQuality} />
        <DetailRow label={t('decisionSignals.marketPhase')} value={item.marketPhase || '-'} />
        <DetailRow label={t('decisionSignals.sourceReport')} value={item.sourceReportId ? `#${item.sourceReportId}` : '-'} />
        <DetailRow label={t('decisionSignals.createdAt')} value={formatDateTime(item.createdAt, language)} />
        <DetailRow label={t('decisionSignals.expiresAt')} value={formatDateTime(item.expiresAt, language)} />
      </div>

      <Card title={t('decisionSignals.pricePlan')} padding="sm" className="rounded-xl">
        <div className="grid gap-3 sm:grid-cols-3">
          <DetailRow label={t('decisionSignals.entryRange')} value={entryRange} />
          <DetailRow label={t('decisionSignals.stopLoss')} value={formatNumber(item.stopLoss)} />
          <DetailRow label={t('decisionSignals.targetPrice')} value={formatNumber(item.targetPrice)} />
        </div>
      </Card>

      <Card title={t('decisionSignals.outcomes')} padding="sm" className="rounded-xl">
        {outcomesLoading ? (
          <p className="text-sm text-secondary-text">{t('common.loading')}...</p>
        ) : outcomesError ? (
          <p className="text-sm text-danger">{outcomesError}</p>
        ) : outcomes.length === 0 ? (
          <p className="text-sm text-secondary-text">{t('decisionSignals.noOutcomes')}</p>
        ) : (
          <div className="grid gap-3">
            {outcomes.map((outcome) => (
              <div key={outcome.id} className="rounded-xl border border-border/60 bg-elevated/40 px-3 py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">{outcome.horizon}</span>
                    {outcome.outcome ? (
                      <Badge variant={OUTCOME_VARIANTS[outcome.outcome]}>
                        {getOutcomeLabel(outcome.outcome, t)}
                      </Badge>
                    ) : (
                      <Badge variant="warning">{t('decisionSignals.outcome.unable')}</Badge>
                    )}
                  </div>
                  <span className="text-xs text-secondary-text">{outcome.engineVersion}</span>
                </div>
                <div className="mt-3 grid gap-2 sm:grid-cols-3">
                  <DetailRow label={t('decisionSignals.returnPct')} value={formatPercent(outcome.stockReturnPct)} />
                  <DetailRow label={t('decisionSignals.directionExpected')} value={outcome.directionExpected || '-'} />
                  <DetailRow label={t('decisionSignals.unableReason')} value={outcome.unableReason || '-'} />
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={t('decisionSignals.feedbackTitle')} padding="sm" className="rounded-xl">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p className="text-sm text-foreground">
              {feedbackLoading ? `${t('common.loading')}...` : getFeedbackLabel(feedback?.feedbackValue, t)}
            </p>
            {feedback?.reasonCode ? (
              <p className="mt-1 text-xs text-secondary-text">{feedback.reasonCode}</p>
            ) : null}
            {feedbackError ? <p className="mt-2 text-sm text-danger">{feedbackError}</p> : null}
          </div>
          {onFeedbackSubmit ? (
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                className="btn-secondary !px-3 !py-1.5 !text-xs"
                disabled={feedbackSaving}
                onClick={() => onFeedbackSubmit('useful')}
              >
                {t('decisionSignals.feedback.useful')}
              </button>
              <button
                type="button"
                className="btn-secondary !px-3 !py-1.5 !text-xs"
                disabled={feedbackSaving}
                onClick={() => onFeedbackSubmit('not_useful')}
              >
                {t('decisionSignals.feedback.not_useful')}
              </button>
            </div>
          ) : null}
        </div>
      </Card>

      <Card padding="sm" className="rounded-xl">
        <div className="grid gap-4">
          <DetailRow label={t('decisionSignals.reason')} value={formatJsonish(item.reason)} />
          <DetailRow label={t('decisionSignals.riskSummary')} value={formatJsonish(item.riskSummary)} />
          <DetailRow label={t('decisionSignals.watchConditions')} value={formatJsonish(item.watchConditions)} />
        </div>
      </Card>

      {evidenceData ? (
        <Card title={t('decisionSignals.evidence')} padding="sm" className="rounded-xl">
          <JsonViewer data={evidenceData} maxHeight="240px" />
        </Card>
      ) : null}
      {qualityData ? (
        <Card title={t('decisionSignals.dataQuality')} padding="sm" className="rounded-xl">
          <JsonViewer data={qualityData} maxHeight="240px" />
        </Card>
      ) : null}
      {metadataData ? (
        <Card title={t('decisionSignals.metadata')} padding="sm" className="rounded-xl">
          <JsonViewer data={metadataData} maxHeight="240px" />
        </Card>
      ) : null}
    </div>
  );
};

type PortfolioSignalSummaryProps = {
  item?: DecisionSignalItem;
  loading?: boolean;
};

export const PortfolioSignalSummary: React.FC<PortfolioSignalSummaryProps> = ({ item, loading = false }) => {
  const { t } = useUiLanguage();
  if (loading && !item) {
    return <span className="text-xs text-secondary-text">{t('decisionSignals.portfolioLoading')}</span>;
  }
  if (!item) {
    return <span className="text-xs text-muted-text">{t('decisionSignals.portfolioEmpty')}</span>;
  }
  const actionLabel = getActionLabel(item, t);
  return (
    <div className="min-w-[11rem] max-w-[18rem] text-left">
      <div className="flex flex-wrap items-center justify-end gap-1.5">
        <Badge variant={getActionVariant(item)}>{actionLabel}</Badge>
        {item.horizon ? <span className="text-[11px] text-secondary-text">{item.horizon}</span> : null}
      </div>
      {item.riskSummary ? <p className="mt-1 line-clamp-2 text-[11px] text-warning">{item.riskSummary}</p> : null}
      {item.watchConditions ? <p className="mt-1 line-clamp-2 text-[11px] text-secondary-text">{item.watchConditions}</p> : null}
    </div>
  );
};
