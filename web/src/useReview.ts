import { useCallback, useEffect, useState } from "react";

import { ApiError, fetchReview, fetchTradeDecision } from "./api";
import type { DeskReport, TradeReview } from "./types";

export interface OpenedDecision {
  tradeNumber: number;
  /** Null while the report is loading or when it could not be loaded. */
  report: DeskReport | null;
  error: string | null;
}

export interface ReviewState {
  review: TradeReview | null;
  opened: OpenedDecision | null;
  openDecision: (tradeNumber: number) => void;
  closeDecision: () => void;
}

/** Loads the trade review, and again whenever the run or its number of closed trades
 *  changes. A failed load keeps the last review: the page shows what it has. */
export function useReview(runId: string | null, closedTradeCount: number): ReviewState {
  const [review, setReview] = useState<TradeReview | null>(null);
  const [opened, setOpened] = useState<OpenedDecision | null>(null);

  useEffect(() => {
    let isCurrent = true;
    setOpened(null);
    fetchReview().then(
      (response) => {
        // An answer for an earlier run, or one that is not a review, is dropped.
        if (isCurrent && Array.isArray(response?.review?.trades)) setReview(response.review);
      },
      () => undefined,
    );
    return () => {
      isCurrent = false;
    };
  }, [runId, closedTradeCount]);

  const openDecision = useCallback((tradeNumber: number) => {
    setOpened({ tradeNumber, report: null, error: null });
    fetchTradeDecision(tradeNumber).then(
      (report) =>
        setOpened((current) =>
          current?.tradeNumber === tradeNumber ? { tradeNumber, report, error: null } : current,
        ),
      (error: unknown) =>
        setOpened((current) =>
          current?.tradeNumber === tradeNumber
            ? {
                tradeNumber,
                report: null,
                error: error instanceof ApiError ? error.message : "The request failed.",
              }
            : current,
        ),
    );
  }, []);
  const closeDecision = useCallback(() => setOpened(null), []);

  return { review, opened, openDecision, closeDecision };
}
