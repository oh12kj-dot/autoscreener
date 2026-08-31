import { lazy, Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { CurrencyProvider } from "./currency";
import { RankingPage } from "./pages/RankingPage";

const AlertsPage = lazy(() => import("./pages/AlertsPage").then((module) => ({ default: module.AlertsPage })));
const CalendarPage = lazy(() => import("./pages/CalendarPage").then((module) => ({ default: module.CalendarPage })));
const DataCoveragePage = lazy(() => import("./pages/DataCoveragePage").then((module) => ({ default: module.DataCoveragePage })));
const ExcludedPage = lazy(() => import("./pages/ExcludedPage").then((module) => ({ default: module.ExcludedPage })));
const GlossaryPage = lazy(() => import("./pages/GlossaryPage").then((module) => ({ default: module.GlossaryPage })));
const LlmReportPage = lazy(() => import("./pages/LlmReportPage").then((module) => ({ default: module.LlmReportPage })));
const MacroPage = lazy(() => import("./pages/MacroPage").then((module) => ({ default: module.MacroPage })));
const PipelinePage = lazy(() => import("./pages/PipelinePage").then((module) => ({ default: module.PipelinePage })));
const PositionsPage = lazy(() => import("./pages/PositionsPage").then((module) => ({ default: module.PositionsPage })));
const RankChangesPage = lazy(() => import("./pages/RankChangesPage").then((module) => ({ default: module.RankChangesPage })));
const ScoreReferencePage = lazy(() => import("./pages/ScoreReferencePage").then((module) => ({ default: module.ScoreReferencePage })));
const TickerDetailPage = lazy(() => import("./pages/TickerDetailPage").then((module) => ({ default: module.TickerDetailPage })));
const ValidationPage = lazy(() => import("./pages/ValidationPage").then((module) => ({ default: module.ValidationPage })));
const WatchlistPage = lazy(() => import("./pages/WatchlistPage").then((module) => ({ default: module.WatchlistPage })));

export default function App() {
  return (
    <BrowserRouter>
      <CurrencyProvider>
        <Suspense fallback={<p>読み込み中...</p>}>
          <Routes>
            <Route element={<Layout />}>
              <Route index element={<RankingPage />} />
              <Route path="candidates/:ticker" element={<TickerDetailPage />} />
              <Route path="watchlist" element={<WatchlistPage />} />
              <Route path="rank-changes" element={<RankChangesPage />} />
              <Route path="excluded" element={<ExcludedPage />} />
              <Route path="positions" element={<PositionsPage />} />
              <Route path="calendar" element={<CalendarPage />} />
              <Route path="alerts" element={<AlertsPage />} />
              <Route path="macro" element={<MacroPage />} />
              <Route path="llm-report" element={<LlmReportPage />} />
              <Route path="glossary" element={<GlossaryPage />} />
              <Route path="reference" element={<ScoreReferencePage />} />
              <Route path="validation" element={<ValidationPage />} />
              <Route path="pipeline" element={<PipelinePage />} />
              <Route path="data-coverage" element={<DataCoveragePage />} />
            </Route>
          </Routes>
        </Suspense>
      </CurrencyProvider>
    </BrowserRouter>
  );
}
