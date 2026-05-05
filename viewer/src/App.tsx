import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "@/components/Layout";
import RecordsPage from "@/pages/RecordsPage";
import RecordDetailPage from "@/pages/RecordDetailPage";
import PerformersPage from "@/pages/PerformersPage";
import PerformerDetailPage from "@/pages/PerformerDetailPage";
import StatusPage from "@/pages/StatusPage";
import QueryPage from "@/pages/QueryPage";

export default function App() {
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Navigate to="/records" replace />} />
        <Route path="/records" element={<RecordsPage />} />
        <Route path="/records/:job_id/:result_index" element={<RecordDetailPage />} />
        <Route path="/performers" element={<PerformersPage />} />
        <Route path="/performers/:name" element={<PerformerDetailPage />} />
        <Route path="/status" element={<StatusPage />} />
        <Route path="/query" element={<QueryPage />} />
      </Routes>
    </Layout>
  );
}
