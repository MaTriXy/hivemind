/**
 * DesktopLayout — Desktop layout composition for ProjectView.
 *
 * Renders the desktop-optimized split layout with tab bar, live status strip,
 * tab content on the left, and activity panel on the right.
 * Each tab panel is wrapped in an error boundary for resilience.
 *
 * STATE-01 fix: Consumes ProjectContext instead of receiving 20+ props.
 */

import React from 'react';
import { useProjectContext } from './ProjectContext';
import { PanelErrorBoundary } from './PanelErrorBoundary';

// ── Existing components ──
import ConductorBar from '../ConductorBar';
import PipelinePhases from '../PipelinePhases';
import PlanView from '../PlanView';
import {
  LiveStatusStrip,
  DesktopTabBar,
  HivemindTabContent,
  AgentsTabContent,
} from '../AgentOrchestra';
import ActivityPanel from '../ActivityPanel';
import CodePanel from '../CodePanel';
import ChangesPanel from '../ChangesPanel';
import TracePanel from '../TracePanel';

// ============================================================================
// Component
// ============================================================================

const DesktopLayout = React.memo(function DesktopLayout(): React.ReactElement {
  const {
    project, projectId, connected, orchestratorState, subAgentStates,
    agentStateList, agentStates, loopProgress, activities, files, sdkCalls,
    liveAgentStream, now, lastTicker, dagGraph, dagTaskStatus, healingEvents,
    desktopTab, selectedAgent, hasMoreMessages, message, agentMetrics,
    onSetDesktopTab, onSelectAgent, onLoadMore, onPause, onResume, onStop,
    onSend, onShowClearConfirm,
  } = useProjectContext();

  return (
    <div className="hidden lg:flex flex-col h-full w-full overflow-hidden">
      <ConductorBar
        projectId={project.project_id}
        projectName={project.project_name}
        status={project.status}
        connected={connected}
        orchestrator={orchestratorState}
        progress={loopProgress}
        totalCost={project.total_cost_usd}
        agentSummary={subAgentStates}
        lastTicker={lastTicker}
      />

      <PipelinePhases
        orchestrator={orchestratorState}
        status={project.status}
        now={now}
      />

      <DesktopTabBar
        desktopTab={desktopTab}
        onSetDesktopTab={onSetDesktopTab}
        projectStatus={project.status}
        activitiesCount={activities.length}
        onShowClearConfirm={onShowClearConfirm}
      />

      <LiveStatusStrip
        orchestratorState={orchestratorState}
        subAgentStates={subAgentStates}
        now={now}
        lastTicker={lastTicker}
      />

      {/* Split view: tab content (left) + activity log (right) */}
      <div
        className="flex-1 flex min-h-0 overflow-hidden"
        style={{ width: '100%' }}
      >
        <div
          className="overflow-y-auto overflow-x-hidden min-w-0"
          style={{ width: '65%', maxWidth: '65%', flexShrink: 0 }}
        >
          {desktopTab === 'hivemind' && (
            <PanelErrorBoundary panelName="Hivemind">
              <HivemindTabContent
                agentStateList={agentStateList}
                loopProgress={loopProgress}
                activities={activities}
                totalCost={project.total_cost_usd}
                projectStatus={project.status}
                messageDraft={message}
                dagGraph={dagGraph}
                dagTaskStatus={dagTaskStatus}
                healingEvents={healingEvents}
              />
            </PanelErrorBoundary>
          )}
          {desktopTab === 'agents' && (
            <PanelErrorBoundary panelName="Agents">
              <AgentsTabContent
                agentStateList={agentStateList}
                selectedAgent={selectedAgent}
                onSelectAgent={onSelectAgent}
                agentMetrics={agentMetrics}
              />
            </PanelErrorBoundary>
          )}
          {desktopTab === 'plan' && (
            <PanelErrorBoundary panelName="Plan">
              <PlanView
                activities={activities}
                dagGraph={dagGraph}
                dagTaskStatus={dagTaskStatus}
              />
            </PanelErrorBoundary>
          )}
          {desktopTab === 'code' && (
            <PanelErrorBoundary panelName="Code">
              <CodePanel projectId={projectId} />
            </PanelErrorBoundary>
          )}
          {desktopTab === 'diff' && (
            <PanelErrorBoundary panelName="Diff">
              <ChangesPanel files={files} variant="desktop" />
            </PanelErrorBoundary>
          )}
          {desktopTab === 'trace' && (
            <PanelErrorBoundary panelName="Trace">
              <TracePanel calls={sdkCalls} variant="desktop" />
            </PanelErrorBoundary>
          )}
        </div>

        <PanelErrorBoundary panelName="Activity">
          <ActivityPanel
            projectId={projectId}
            agentStates={agentStates}
            liveAgentStream={liveAgentStream}
            now={now}
            activities={activities}
            hasMoreMessages={hasMoreMessages}
            onLoadMore={onLoadMore}
            projectStatus={project.status}
            onPause={onPause}
            onResume={onResume}
            onStop={onStop}
            onSend={onSend}
          />
        </PanelErrorBoundary>
      </div>
    </div>
  );
});

export default DesktopLayout;
