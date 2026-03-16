/**
 * project/ — Barrel export for all ProjectView sub-components.
 */

export { default as ProjectContext, useProjectContext } from './ProjectContext';
export type { ProjectContextValue } from './ProjectContext';

export { default as ClearHistoryModal } from './ClearHistoryModal';
export type { ClearHistoryModalProps } from './ClearHistoryModal';

export { default as MobileLayout } from './MobileLayout';

export { default as DesktopLayout } from './DesktopLayout';

export { PanelErrorBoundary } from './PanelErrorBoundary';
export type { PanelErrorBoundaryProps } from './PanelErrorBoundary';

export {
  ActivityFeedSkeleton,
  CodePanelSkeleton,
  AgentCardSkeleton,
  TracePanelSkeleton,
} from './PanelLoadingSkeleton';

export { default as EmptyState } from './EmptyState';
export type { EmptyStateProps } from './EmptyState';
export {
  EmptyActivityState,
  EmptyCodeState,
  EmptyTraceState,
  EmptyPlanState,
  EmptyChangesState,
} from './EmptyState';
