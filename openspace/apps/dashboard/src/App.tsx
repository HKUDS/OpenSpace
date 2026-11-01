import { Navigate, RouterProvider, createBrowserRouter } from 'react-router-dom';
import MainLayout from './layouts/MainLayout';
import DashboardPage from './pages/DashboardPage';
import EvolutionPage from './pages/EvolutionPage';
import SkillsPage from './pages/SkillsPage';
import SkillDetailPage from './pages/SkillDetailPage';
import WorkflowsPage from './pages/WorkflowsPage';
import WorkflowDetailPage from './pages/WorkflowDetailPage';
import AgentTracePage from './pages/AgentTracePage';

const router = createBrowserRouter([
  {
    path: '/',
    element: <MainLayout />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: 'dashboard', element: <DashboardPage /> },
      { path: 'evolution', element: <EvolutionPage /> },
      { path: 'skills', element: <SkillsPage /> },
      { path: 'skills/:skillId', element: <SkillDetailPage /> },
      { path: 'workflows', element: <WorkflowsPage /> },
      { path: 'workflows/:workflowId', element: <WorkflowDetailPage /> },
      { path: 'workflows/:workflowId/trace', element: <AgentTracePage /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
