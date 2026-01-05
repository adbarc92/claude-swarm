"""
AppForge MCP Server
Production-grade state management for AI-orchestrated app development

This MCP server provides persistent state management, dependency validation,
approval workflow, and audit logging for the AppForge system.

Installation:
    pip install mcp sqlite3

Configuration in Claude Code settings.json:
    {
    "mcpServers": {
        "appforge": {
        "command": "uv",
        "args": ["run", "python", "appforge_mcp_server.py"]
        }
    }
    }

Usage:
    python appforge_mcp_server.py
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional
from contextlib import contextmanager

from mcp.server import Server
from mcp.types import Tool, TextContent, ImageContent, EmbeddedResource
from mcp.server.stdio import stdio_server

# ============================================================================
# Database Schema & State Manager
# ============================================================================


class AppForgeDB:
    """Manages SQLite database for AppForge state"""

    def __init__(self, db_path: str = "appforge.db"):
        self.db_path = db_path
        self.init_schema()

    @contextmanager
    def get_connection(self):
        """Context manager for database connections"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # Enable column access by name
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def init_schema(self):
        """Initialize database schema"""
        with self.get_connection() as conn:
            # Projects table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    tech_stack TEXT DEFAULT 'default',
                    current_phase INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'paused', 'completed', 'failed')),
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Phases table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS phases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    phase_number INTEGER NOT NULL,
                    phase_name TEXT NOT NULL,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'complete', 'blocked')),
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                    UNIQUE(project_id, phase_number)
                )
            """)

            # Agents table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    phase_number INTEGER NOT NULL,
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'complete', 'failed')),
                    output_artifacts TEXT,
                    error_message TEXT,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)

            # Features table (for Phase 4 iteration)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS features (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    feature_name TEXT NOT NULL,
                    description TEXT,
                    priority TEXT DEFAULT 'MEDIUM' CHECK(priority IN ('HIGH', 'MEDIUM', 'LOW')),
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'in_progress', 'complete', 'failed', 'skipped')),
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    assigned_iteration INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)

            # Approval gates table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approval_gates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    gate_name TEXT NOT NULL,
                    gate_type TEXT DEFAULT 'must_approve' CHECK(gate_type IN ('must_approve', 'optional_review', 'auto_proceed')),
                    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
                    artifacts TEXT,
                    user_feedback TEXT,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved_at TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)

            # Artifacts table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    agent_name TEXT NOT NULL,
                    artifact_type TEXT NOT NULL,
                    artifact_name TEXT NOT NULL,
                    file_path TEXT,
                    content TEXT,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)

            # Audit log table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    agent_name TEXT,
                    phase_number INTEGER,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
                )
            """)

            # Dependency graph table (for validation)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dependencies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT UNIQUE NOT NULL,
                    depends_on TEXT NOT NULL,
                    phase_number INTEGER NOT NULL
                )
            """)

            # Initialize dependency graph if empty
            cursor = conn.execute("SELECT COUNT(*) FROM dependencies")
            if cursor.fetchone()[0] == 0:
                self._initialize_dependency_graph(conn)

    def _initialize_dependency_graph(self, conn):
        """Initialize the dependency graph for agent orchestration"""
        dependencies = [
            # Phase 0
            ("input-agent", "[]", 0),
            # Phase 1 (parallel)
            ("requirements-analyst", '["input-agent"]', 1),
            ("ui-ux-designer", '["input-agent"]', 1),
            # Phase 2 (parallel, depends on Phase 1)
            ("database-architect", '["requirements-analyst", "ui-ux-designer"]', 2),
            ("api-designer", '["requirements-analyst", "ui-ux-designer"]', 2),
            ("integration-specialist", '["requirements-analyst", "ui-ux-designer"]', 2),
            # Phase 3 (sequential)
            (
                "backend-developer",
                '["database-architect", "api-designer", "integration-specialist"]',
                3,
            ),
            ("frontend-developer", '["backend-developer", "ui-ux-designer"]', 3),
            # Phase 4 (feature loop - same as Phase 3 but iterative)
            ("backend-developer-feature", '["backend-developer"]', 4),
            ("frontend-developer-feature", '["backend-developer-feature"]', 4),
            (
                "qa-engineer-feature",
                '["backend-developer-feature", "frontend-developer-feature"]',
                4,
            ),
            # Phase 5 (parallel, depends on all features)
            ("qa-engineer", '["qa-engineer-feature"]', 5),
            ("security-auditor", '["qa-engineer-feature"]', 5),
            ("devops-engineer", '["qa-engineer-feature"]', 5),
            # Phase 6 (sequential deployment)
            (
                "devops-engineer-staging",
                '["qa-engineer", "security-auditor", "devops-engineer"]',
                6,
            ),
            ("devops-engineer-production", '["devops-engineer-staging"]', 6),
            ("devops-engineer-appstore", '["devops-engineer-production"]', 6),
        ]

        conn.executemany(
            "INSERT INTO dependencies (agent_name, depends_on, phase_number) VALUES (?, ?, ?)",
            dependencies,
        )

    def log_event(
        self,
        conn,
        project_id: int,
        event_type: str,
        details: Dict[str, Any],
        agent_name: str = None,
        phase_number: int = None,
    ):
        """Log an event to the audit log"""
        conn.execute(
            """INSERT INTO audit_log (project_id, event_type, agent_name, phase_number, details)
               VALUES (?, ?, ?, ?, ?)""",
            (project_id, event_type, agent_name, phase_number, json.dumps(details)),
        )


# ============================================================================
# AppForge State Manager
# ============================================================================


class AppForgeStateManager:
    """High-level state management operations"""

    PHASE_NAMES = {
        0: "Input Gathering",
        1: "Discovery",
        2: "Technical Design",
        3: "Foundation",
        4: "Feature Development",
        5: "Hardening",
        6: "Deployment",
    }

    def __init__(self, db: AppForgeDB):
        self.db = db

    def create_project(
        self, name: str, description: str, tech_stack: str = "default"
    ) -> Dict[str, Any]:
        """Create a new AppForge project"""
        with self.db.get_connection() as conn:
            try:
                cursor = conn.execute(
                    """INSERT INTO projects (name, description, tech_stack, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, description, tech_stack, datetime.now(), datetime.now()),
                )
                project_id = cursor.lastrowid

                # Initialize all phases
                for phase_num, phase_name in self.PHASE_NAMES.items():
                    conn.execute(
                        """INSERT INTO phases (project_id, phase_number, phase_name, status)
                           VALUES (?, ?, ?, ?)""",
                        (project_id, phase_num, phase_name, "pending"),
                    )

                # Mark Phase 0 as in_progress
                conn.execute(
                    """UPDATE phases SET status = 'in_progress', started_at = ?
                       WHERE project_id = ? AND phase_number = 0""",
                    (datetime.now(), project_id),
                )

                # Log creation
                self.db.log_event(
                    conn,
                    project_id,
                    "project_created",
                    {
                        "name": name,
                        "description": description,
                        "tech_stack": tech_stack,
                    },
                )

                return {
                    "success": True,
                    "project_id": project_id,
                    "name": name,
                    "message": f"Project '{name}' created successfully. Starting Phase 0: Input Gathering",
                }
            except sqlite3.IntegrityError:
                return {"success": False, "error": f"Project '{name}' already exists"}

    def get_project_state(self, project_id: int) -> Dict[str, Any]:
        """Get complete state of a project"""
        with self.db.get_connection() as conn:
            # Get project info
            project = conn.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()

            if not project:
                return {"success": False, "error": "Project not found"}

            # Get phases
            phases = conn.execute(
                "SELECT * FROM phases WHERE project_id = ? ORDER BY phase_number",
                (project_id,),
            ).fetchall()

            # Get completed agents
            agents = conn.execute(
                """SELECT * FROM agents WHERE project_id = ?
                   ORDER BY completed_at DESC""",
                (project_id,),
            ).fetchall()

            # Get pending approvals
            approvals = conn.execute(
                """SELECT * FROM approval_gates WHERE project_id = ? AND status = 'pending'
                   ORDER BY requested_at DESC""",
                (project_id,),
            ).fetchall()

            # Get features
            features = conn.execute(
                """SELECT * FROM features WHERE project_id = ?
                   ORDER BY priority DESC, id ASC""",
                (project_id,),
            ).fetchall()

            # Get recent audit log entries
            audit_log = conn.execute(
                """SELECT * FROM audit_log WHERE project_id = ?
                   ORDER BY timestamp DESC LIMIT 20""",
                (project_id,),
            ).fetchall()

            return {
                "success": True,
                "project": dict(project),
                "phases": [dict(p) for p in phases],
                "agents": [dict(a) for a in agents],
                "pending_approvals": [dict(a) for a in approvals],
                "features": [dict(f) for f in features],
                "recent_activity": [dict(log) for log in audit_log],
            }

    def can_start_agent(self, project_id: int, agent_name: str) -> Dict[str, Any]:
        """Check if prerequisites are met for an agent to start"""
        with self.db.get_connection() as conn:
            # Get agent dependencies
            dep_row = conn.execute(
                "SELECT depends_on, phase_number FROM dependencies WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()

            if not dep_row:
                return {"success": False, "error": f"Unknown agent: {agent_name}"}

            dependencies = json.loads(dep_row["depends_on"])
            required_phase = dep_row["phase_number"]

            # Get completed agents
            completed = conn.execute(
                """SELECT agent_name FROM agents
                   WHERE project_id = ? AND status = 'complete'""",
                (project_id,),
            ).fetchall()
            completed_names = [row["agent_name"] for row in completed]

            # Check if all dependencies are met
            missing = [dep for dep in dependencies if dep not in completed_names]
            can_start = len(missing) == 0

            # Get current phase
            project = conn.execute(
                "SELECT current_phase FROM projects WHERE id = ?", (project_id,)
            ).fetchone()

            current_phase = project["current_phase"] if project else 0

            # Check if we're in the right phase
            if can_start and current_phase < required_phase:
                can_start = False
                missing.append(
                    f"Project is in Phase {current_phase}, but agent requires Phase {required_phase}"
                )

            return {
                "success": True,
                "can_start": can_start,
                "agent_name": agent_name,
                "required_phase": required_phase,
                "current_phase": current_phase,
                "dependencies": dependencies,
                "missing_dependencies": missing,
                "message": "✅ Ready to start"
                if can_start
                else f"❌ Missing: {', '.join(missing)}",
            }

    def mark_agent_complete(
        self, project_id: int, agent_name: str, artifacts: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Mark an agent as complete with its output artifacts"""
        with self.db.get_connection() as conn:
            # Get agent's phase
            dep_row = conn.execute(
                "SELECT phase_number FROM dependencies WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()

            if not dep_row:
                return {"success": False, "error": f"Unknown agent: {agent_name}"}

            phase_number = dep_row["phase_number"]

            # Check if agent already exists (for feature iterations)
            existing = conn.execute(
                """SELECT id, status FROM agents
                   WHERE project_id = ? AND agent_name = ? AND status != 'complete'""",
                (project_id, agent_name),
            ).fetchone()

            if existing:
                # Update existing record
                conn.execute(
                    """UPDATE agents
                       SET status = 'complete',
                           output_artifacts = ?,
                           completed_at = ?
                       WHERE id = ?""",
                    (json.dumps(artifacts), datetime.now(), existing["id"]),
                )
            else:
                # Insert new record
                conn.execute(
                    """INSERT INTO agents (project_id, agent_name, phase_number, status,
                                          output_artifacts, completed_at)
                       VALUES (?, ?, ?, 'complete', ?, ?)""",
                    (
                        project_id,
                        agent_name,
                        phase_number,
                        json.dumps(artifacts),
                        datetime.now(),
                    ),
                )

            # Update project timestamp
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (datetime.now(), project_id),
            )

            # Log completion
            self.db.log_event(
                conn,
                project_id,
                "agent_complete",
                {
                    "agent_name": agent_name,
                    "phase_number": phase_number,
                    "artifacts": artifacts,
                },
                agent_name=agent_name,
                phase_number=phase_number,
            )

            return {
                "success": True,
                "message": f"✅ {agent_name} marked complete",
                "phase": phase_number,
            }

    def mark_agent_failed(
        self, project_id: int, agent_name: str, error: str
    ) -> Dict[str, Any]:
        """Mark an agent as failed"""
        with self.db.get_connection() as conn:
            conn.execute(
                """INSERT INTO agents (project_id, agent_name, status, error_message, completed_at)
                   VALUES (?, ?, 'failed', ?, ?)""",
                (project_id, agent_name, error, datetime.now()),
            )

            self.db.log_event(
                conn,
                project_id,
                "agent_failed",
                {"agent_name": agent_name, "error": error},
                agent_name=agent_name,
            )

            return {"success": True, "message": f"Agent {agent_name} marked as failed"}

    def get_next_agents(self, project_id: int) -> Dict[str, Any]:
        """Get list of agents that can be started next"""
        with self.db.get_connection() as conn:
            # Get all agent names from dependencies
            all_agents = conn.execute(
                "SELECT agent_name FROM dependencies ORDER BY phase_number, agent_name"
            ).fetchall()

            ready_agents = []
            blocked_agents = []

            for row in all_agents:
                agent_name = row["agent_name"]
                check = self.can_start_agent(project_id, agent_name)

                if check.get("can_start"):
                    # Check if not already complete
                    completed = conn.execute(
                        """SELECT id FROM agents
                           WHERE project_id = ? AND agent_name = ? AND status = 'complete'""",
                        (project_id, agent_name),
                    ).fetchone()

                    if not completed:
                        ready_agents.append(
                            {"name": agent_name, "phase": check["required_phase"]}
                        )
                elif check.get("missing_dependencies"):
                    blocked_agents.append(
                        {
                            "name": agent_name,
                            "phase": check["required_phase"],
                            "missing": check["missing_dependencies"],
                        }
                    )

            return {
                "success": True,
                "ready_agents": ready_agents,
                "blocked_agents": blocked_agents[:5],  # Show first 5 blocked
            }

    def request_approval(
        self, project_id: int, gate_name: str, gate_type: str, artifacts: List[str]
    ) -> Dict[str, Any]:
        """Request user approval at a gate"""
        with self.db.get_connection() as conn:
            cursor = conn.execute(
                """INSERT INTO approval_gates (project_id, gate_name, gate_type,
                                               artifacts, requested_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    project_id,
                    gate_name,
                    gate_type,
                    json.dumps(artifacts),
                    datetime.now(),
                ),
            )

            gate_id = cursor.lastrowid

            self.db.log_event(
                conn,
                project_id,
                "approval_requested",
                {
                    "gate_name": gate_name,
                    "gate_type": gate_type,
                    "artifacts": artifacts,
                },
            )

            return {
                "success": True,
                "gate_id": gate_id,
                "gate_name": gate_name,
                "gate_type": gate_type,
                "message": f"🛑 Approval requested: {gate_name}",
            }

    def record_approval(
        self, project_id: int, gate_name: str, approved: bool, feedback: str = None
    ) -> Dict[str, Any]:
        """Record user's approval decision"""
        with self.db.get_connection() as conn:
            status = "approved" if approved else "rejected"

            conn.execute(
                """UPDATE approval_gates
                   SET status = ?, user_feedback = ?, resolved_at = ?
                   WHERE project_id = ? AND gate_name = ? AND status = 'pending'""",
                (status, feedback, datetime.now(), project_id, gate_name),
            )

            # If this was a phase gate and approved, advance phase
            if approved and "Gate" in gate_name:
                gate_number = (
                    int(gate_name.split()[1]) if len(gate_name.split()) > 1 else None
                )
                if gate_number:
                    next_phase = gate_number
                    conn.execute(
                        """UPDATE projects SET current_phase = ?, updated_at = ?
                           WHERE id = ?""",
                        (next_phase, datetime.now(), project_id),
                    )

                    conn.execute(
                        """UPDATE phases SET status = 'in_progress', started_at = ?
                           WHERE project_id = ? AND phase_number = ?""",
                        (datetime.now(), project_id, next_phase),
                    )

            self.db.log_event(
                conn,
                project_id,
                "approval_recorded",
                {"gate_name": gate_name, "approved": approved, "feedback": feedback},
            )

            return {
                "success": True,
                "approved": approved,
                "message": f"✅ Approved: {gate_name}"
                if approved
                else f"❌ Rejected: {gate_name}",
            }

    def add_features(
        self, project_id: int, features: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Add features to the backlog"""
        with self.db.get_connection() as conn:
            for feature in features:
                conn.execute(
                    """INSERT INTO features (project_id, feature_name, description, priority)
                       VALUES (?, ?, ?, ?)""",
                    (
                        project_id,
                        feature["name"],
                        feature.get("description", ""),
                        feature.get("priority", "MEDIUM"),
                    ),
                )

            self.db.log_event(
                conn,
                project_id,
                "features_added",
                {"count": len(features), "features": features},
            )

            return {
                "success": True,
                "added": len(features),
                "message": f"✅ Added {len(features)} features to backlog",
            }

    def get_next_feature(self, project_id: int) -> Dict[str, Any]:
        """Get the next feature to implement"""
        with self.db.get_connection() as conn:
            feature = conn.execute(
                """SELECT * FROM features
                   WHERE project_id = ? AND status = 'pending'
                   ORDER BY
                       CASE priority
                           WHEN 'HIGH' THEN 1
                           WHEN 'MEDIUM' THEN 2
                           WHEN 'LOW' THEN 3
                       END,
                       id ASC
                   LIMIT 1""",
                (project_id,),
            ).fetchone()

            if not feature:
                return {
                    "success": True,
                    "has_next": False,
                    "message": "🎉 All features complete!",
                }

            return {"success": True, "has_next": True, "feature": dict(feature)}

    def mark_feature_complete(self, project_id: int, feature_id: int) -> Dict[str, Any]:
        """Mark a feature as complete"""
        with self.db.get_connection() as conn:
            conn.execute(
                """UPDATE features
                   SET status = 'complete', completed_at = ?
                   WHERE id = ?""",
                (datetime.now(), feature_id),
            )

            feature = conn.execute(
                "SELECT feature_name FROM features WHERE id = ?", (feature_id,)
            ).fetchone()

            self.db.log_event(
                conn,
                project_id,
                "feature_complete",
                {
                    "feature_id": feature_id,
                    "feature_name": feature["feature_name"] if feature else "Unknown",
                },
            )

            return {
                "success": True,
                "message": f"✅ Feature complete: {feature['feature_name'] if feature else feature_id}",
            }

    def record_feature_retry(self, project_id: int, feature_id: int) -> Dict[str, Any]:
        """Record a retry attempt for a feature"""
        with self.db.get_connection() as conn:
            conn.execute(
                """UPDATE features
                   SET retry_count = retry_count + 1
                   WHERE id = ?""",
                (feature_id,),
            )

            feature = conn.execute(
                "SELECT feature_name, retry_count, max_retries FROM features WHERE id = ?",
                (feature_id,),
            ).fetchone()

            if feature:
                retries_left = feature["max_retries"] - feature["retry_count"]

                self.db.log_event(
                    conn,
                    project_id,
                    "feature_retry",
                    {
                        "feature_id": feature_id,
                        "feature_name": feature["feature_name"],
                        "retry_count": feature["retry_count"],
                        "retries_left": retries_left,
                    },
                )

                return {
                    "success": True,
                    "retry_count": feature["retry_count"],
                    "retries_left": retries_left,
                    "max_retries_reached": retries_left <= 0,
                    "message": f"⚠️ Retry {feature['retry_count']}/{feature['max_retries']} for {feature['feature_name']}",
                }

            return {"success": False, "error": "Feature not found"}

    def get_project_progress(self, project_id: int) -> Dict[str, Any]:
        """Get project completion percentage and status"""
        with self.db.get_connection() as conn:
            project = conn.execute(
                "SELECT current_phase, status FROM projects WHERE id = ?", (project_id,)
            ).fetchone()

            if not project:
                return {"success": False, "error": "Project not found"}

            # Count completed agents
            completed_agents = conn.execute(
                """SELECT COUNT(*) as count FROM agents
                   WHERE project_id = ? AND status = 'complete'""",
                (project_id,),
            ).fetchone()["count"]

            # Total expected agents (rough estimate: 3-5 per phase * 7 phases)
            total_agents = 25

            # Count features
            total_features = conn.execute(
                "SELECT COUNT(*) as count FROM features WHERE project_id = ?",
                (project_id,),
            ).fetchone()["count"]

            completed_features = conn.execute(
                """SELECT COUNT(*) as count FROM features
                   WHERE project_id = ? AND status = 'complete'""",
                (project_id,),
            ).fetchone()["count"]

            # Calculate progress
            phase_progress = (project["current_phase"] / 6) * 100
            agent_progress = (completed_agents / total_agents) * 100
            feature_progress = (
                (completed_features / total_features * 100) if total_features > 0 else 0
            )

            # Weighted average
            overall_progress = (
                phase_progress * 0.4 + agent_progress * 0.3 + feature_progress * 0.3
            )

            return {
                "success": True,
                "project_id": project_id,
                "current_phase": project["current_phase"],
                "phase_name": self.PHASE_NAMES.get(project["current_phase"], "Unknown"),
                "project_status": project["status"],
                "progress_percentage": round(overall_progress, 1),
                "completed_agents": completed_agents,
                "total_agents": total_agents,
                "completed_features": completed_features,
                "total_features": total_features,
            }

    def list_projects(self) -> Dict[str, Any]:
        """List all projects"""
        with self.db.get_connection() as conn:
            projects = conn.execute(
                """SELECT id, name, description, current_phase, status,
                          created_at, updated_at
                   FROM projects
                   ORDER BY updated_at DESC"""
            ).fetchall()

            return {
                "success": True,
                "projects": [dict(p) for p in projects],
                "count": len(projects),
            }

    def save_artifact(
        self,
        project_id: int,
        agent_name: str,
        artifact_type: str,
        artifact_name: str,
        file_path: str = None,
        content: str = None,
        metadata: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        """Save an artifact produced by an agent"""
        with self.db.get_connection() as conn:
            conn.execute(
                """INSERT INTO artifacts (project_id, agent_name, artifact_type,
                                         artifact_name, file_path, content, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    agent_name,
                    artifact_type,
                    artifact_name,
                    file_path,
                    content,
                    json.dumps(metadata) if metadata else None,
                ),
            )

            return {"success": True, "message": f"✅ Artifact saved: {artifact_name}"}

    def get_artifact(self, project_id: int, artifact_name: str) -> Dict[str, Any]:
        """Get an artifact by name"""
        with self.db.get_connection() as conn:
            artifact = conn.execute(
                """SELECT * FROM artifacts
                   WHERE project_id = ? AND artifact_name = ?
                   ORDER BY created_at DESC LIMIT 1""",
                (project_id, artifact_name),
            ).fetchone()

            if not artifact:
                return {"success": False, "error": "Artifact not found"}

            return {"success": True, "artifact": dict(artifact)}

    def list_artifacts(
        self, project_id: int, filter_type: str = None
    ) -> Dict[str, Any]:
        """List artifacts for a project"""
        with self.db.get_connection() as conn:
            if filter_type:
                artifacts = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE project_id = ? AND artifact_type = ?
                       ORDER BY created_at DESC""",
                    (project_id, filter_type),
                ).fetchall()
            else:
                artifacts = conn.execute(
                    """SELECT * FROM artifacts
                       WHERE project_id = ?
                       ORDER BY created_at DESC""",
                    (project_id,),
                ).fetchall()

            return {
                "success": True,
                "artifacts": [dict(a) for a in artifacts],
                "count": len(artifacts),
            }


# ============================================================================
# MCP Server Implementation
# ============================================================================

# Initialize database and state manager
db = AppForgeDB()
state_manager = AppForgeStateManager(db)

# Create MCP server
app = Server("appforge-state-manager")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List all available MCP tools"""
    return [
        # Project Management
        Tool(
            name="appforge_create_project",
            description="Create a new AppForge project with specified name, description, and tech stack",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Unique project name"},
                    "description": {
                        "type": "string",
                        "description": "Project description",
                    },
                    "tech_stack": {
                        "type": "string",
                        "description": "Tech stack identifier (default: 'default')",
                        "default": "default",
                    },
                },
                "required": ["name", "description"],
            },
        ),
        Tool(
            name="appforge_get_project_state",
            description="Get complete state of a project including phases, agents, features, approvals, and recent activity",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"}
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="appforge_list_projects",
            description="List all AppForge projects",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="appforge_get_project_progress",
            description="Get project completion percentage, current phase, and progress metrics",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"}
                },
                "required": ["project_id"],
            },
        ),
        # Agent Management
        Tool(
            name="appforge_can_start_agent",
            description="Check if prerequisites are met for an agent to start. Returns dependencies and blocking reasons if any.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "agent_name": {
                        "type": "string",
                        "description": "Agent name (e.g., 'requirements-analyst', 'database-architect')",
                    },
                },
                "required": ["project_id", "agent_name"],
            },
        ),
        Tool(
            name="appforge_mark_agent_complete",
            description="Mark an agent as complete with its output artifacts",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "agent_name": {"type": "string", "description": "Agent name"},
                    "artifacts": {
                        "type": "object",
                        "description": "Output artifacts produced by the agent",
                    },
                },
                "required": ["project_id", "agent_name", "artifacts"],
            },
        ),
        Tool(
            name="appforge_mark_agent_failed",
            description="Mark an agent as failed with error message",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "agent_name": {"type": "string", "description": "Agent name"},
                    "error": {"type": "string", "description": "Error message"},
                },
                "required": ["project_id", "agent_name", "error"],
            },
        ),
        Tool(
            name="appforge_get_next_agents",
            description="Get list of agents that can be started next, grouped by ready and blocked",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"}
                },
                "required": ["project_id"],
            },
        ),
        # Feature Management
        Tool(
            name="appforge_add_features",
            description="Add features to the project backlog for Phase 4 iteration",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "features": {
                        "type": "array",
                        "description": "List of features to add",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "priority": {
                                    "type": "string",
                                    "enum": ["HIGH", "MEDIUM", "LOW"],
                                },
                            },
                            "required": ["name"],
                        },
                    },
                },
                "required": ["project_id", "features"],
            },
        ),
        Tool(
            name="appforge_get_next_feature",
            description="Get the next feature to implement from the backlog (priority-ordered)",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"}
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="appforge_mark_feature_complete",
            description="Mark a feature as complete",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "feature_id": {"type": "integer", "description": "Feature ID"},
                },
                "required": ["project_id", "feature_id"],
            },
        ),
        Tool(
            name="appforge_record_feature_retry",
            description="Record a retry attempt for a feature (tracks retry count against max retries)",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "feature_id": {"type": "integer", "description": "Feature ID"},
                },
                "required": ["project_id", "feature_id"],
            },
        ),
        # Approval Gates
        Tool(
            name="appforge_request_approval",
            description="Request user approval at a gate (must_approve, optional_review, or auto_proceed)",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "gate_name": {
                        "type": "string",
                        "description": "Name of the approval gate (e.g., 'Gate 1', 'Feature Gate')",
                    },
                    "gate_type": {
                        "type": "string",
                        "enum": ["must_approve", "optional_review", "auto_proceed"],
                        "description": "Type of approval gate",
                    },
                    "artifacts": {
                        "type": "array",
                        "description": "List of artifact names to review",
                        "items": {"type": "string"},
                    },
                },
                "required": ["project_id", "gate_name", "gate_type", "artifacts"],
            },
        ),
        Tool(
            name="appforge_record_approval",
            description="Record user's approval decision (approved or rejected with optional feedback)",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "gate_name": {
                        "type": "string",
                        "description": "Name of the approval gate",
                    },
                    "approved": {
                        "type": "boolean",
                        "description": "Whether the user approved",
                    },
                    "feedback": {
                        "type": "string",
                        "description": "Optional user feedback",
                    },
                },
                "required": ["project_id", "gate_name", "approved"],
            },
        ),
        # Artifact Management
        Tool(
            name="appforge_save_artifact",
            description="Save an artifact produced by an agent",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "agent_name": {
                        "type": "string",
                        "description": "Agent that produced the artifact",
                    },
                    "artifact_type": {
                        "type": "string",
                        "description": "Type of artifact (e.g., 'document', 'code', 'diagram')",
                    },
                    "artifact_name": {
                        "type": "string",
                        "description": "Name of the artifact",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "File path where artifact is saved",
                    },
                    "content": {
                        "type": "string",
                        "description": "Artifact content (for text artifacts)",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Additional metadata about the artifact",
                    },
                },
                "required": [
                    "project_id",
                    "agent_name",
                    "artifact_type",
                    "artifact_name",
                ],
            },
        ),
        Tool(
            name="appforge_get_artifact",
            description="Get an artifact by name",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "artifact_name": {"type": "string", "description": "Artifact name"},
                },
                "required": ["project_id", "artifact_name"],
            },
        ),
        Tool(
            name="appforge_list_artifacts",
            description="List all artifacts for a project, optionally filtered by type",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "integer", "description": "Project ID"},
                    "filter_type": {
                        "type": "string",
                        "description": "Optional: filter by artifact type",
                    },
                },
                "required": ["project_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle MCP tool calls"""

    try:
        # Route to appropriate state manager method
        if name == "appforge_create_project":
            result = state_manager.create_project(
                arguments["name"],
                arguments["description"],
                arguments.get("tech_stack", "default"),
            )

        elif name == "appforge_get_project_state":
            result = state_manager.get_project_state(arguments["project_id"])

        elif name == "appforge_list_projects":
            result = state_manager.list_projects()

        elif name == "appforge_get_project_progress":
            result = state_manager.get_project_progress(arguments["project_id"])

        elif name == "appforge_can_start_agent":
            result = state_manager.can_start_agent(
                arguments["project_id"], arguments["agent_name"]
            )

        elif name == "appforge_mark_agent_complete":
            result = state_manager.mark_agent_complete(
                arguments["project_id"], arguments["agent_name"], arguments["artifacts"]
            )

        elif name == "appforge_mark_agent_failed":
            result = state_manager.mark_agent_failed(
                arguments["project_id"], arguments["agent_name"], arguments["error"]
            )

        elif name == "appforge_get_next_agents":
            result = state_manager.get_next_agents(arguments["project_id"])

        elif name == "appforge_add_features":
            result = state_manager.add_features(
                arguments["project_id"], arguments["features"]
            )

        elif name == "appforge_get_next_feature":
            result = state_manager.get_next_feature(arguments["project_id"])

        elif name == "appforge_mark_feature_complete":
            result = state_manager.mark_feature_complete(
                arguments["project_id"], arguments["feature_id"]
            )

        elif name == "appforge_record_feature_retry":
            result = state_manager.record_feature_retry(
                arguments["project_id"], arguments["feature_id"]
            )

        elif name == "appforge_request_approval":
            result = state_manager.request_approval(
                arguments["project_id"],
                arguments["gate_name"],
                arguments["gate_type"],
                arguments["artifacts"],
            )

        elif name == "appforge_record_approval":
            result = state_manager.record_approval(
                arguments["project_id"],
                arguments["gate_name"],
                arguments["approved"],
                arguments.get("feedback"),
            )

        elif name == "appforge_save_artifact":
            result = state_manager.save_artifact(
                arguments["project_id"],
                arguments["agent_name"],
                arguments["artifact_type"],
                arguments["artifact_name"],
                arguments.get("file_path"),
                arguments.get("content"),
                arguments.get("metadata"),
            )

        elif name == "appforge_get_artifact":
            result = state_manager.get_artifact(
                arguments["project_id"], arguments["artifact_name"]
            )

        elif name == "appforge_list_artifacts":
            result = state_manager.list_artifacts(
                arguments["project_id"], arguments.get("filter_type")
            )

        else:
            result = {"success": False, "error": f"Unknown tool: {name}"}

        # Format response
        response_text = json.dumps(result, indent=2)
        return [TextContent(type="text", text=response_text)]

    except Exception as e:
        error_response = {"success": False, "error": str(e), "tool": name}
        return [TextContent(type="text", text=json.dumps(error_response, indent=2))]


# ============================================================================
# Main Entry Point
# ============================================================================


async def main():
    """Run the AppForge MCP server"""
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
