import { BrowserRouter, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { CurrencyProvider } from "./currency";
import { AlertsPage } from "./pages/AlertsPage";
import { CalendarPage } from "./pages/CalendarPage";
import { ExcludedPage } from "./pages/ExcludedPage";
import { GlossaryPage } from "./pages/GlossaryPage";
import { LlmReportPage } from "./pages/LlmReportPage";
import { MacroPage } from "./pages/MacroPage";
import { PipelinePage } from "./pages/PipelinePage";
import { PositionsPage } from "./pages/PositionsPage";
import { RankChangesPage } from "./pages/RankChangesPage";
import { RankingPage } from "./pages/RankingPage";
import { ScoreReferencePage } from "./pages/ScoreReferencePage";
import { TickerDetailPage } from "./pages/TickerDetailPage";
import { ValidationPage } from "./pages/ValidationPage";
import { WatchlistPage } from "./pages/WatchlistPage";
import { DataCoveragePage } from "./pages/DataCoveragePage";

export default function App() {
  return (
    <BrowserRouter>
      <CurrencyProvider>
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
      </CurrencyProvider>
    </BrowserRouter>
  );
}
