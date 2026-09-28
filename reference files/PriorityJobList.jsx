import React, { useEffect, useState } from "react";
import "./PriorityJobList.css";

/**
 * Small badge shown on Enterprise ("Promoted") and Premium
 * ("Priority listing") cards. Renders nothing for standard/organic jobs.
 */
export function PriorityBadge({ badge }) {
  if (!badge) return null;
  const isEnterprise = badge === "Promoted";
  return (
    <span className={`pjl-badge${isEnterprise ? " pjl-badge--enterprise" : ""}`}>
      {badge}
    </span>
  );
}

/**
 * Drop-in replacement for wherever the marketplace currently fetches and
 * maps its raw job search results. This calls the new priority-aware
 * endpoint instead of the plain search endpoint, and passes each ranked
 * job + its badge into the existing `JobCard` component via `renderJob`
 * — so you don't need to rewrite JobCard, just pass it in.
 *
 * Usage:
 *   <PriorityJobList
 *     filters={{ keywords: "Software Engineer", location: "Bangalore" }}
 *     renderJob={(job, badge) => <JobCard job={job} badgeSlot={<PriorityBadge badge={badge} />} />}
 *   />
 */
export default function PriorityJobList({ filters, renderJob }) {
  const [results, setResults] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    fetch("/api/search/priority", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(filters),
    })
      .then((res) => {
        if (!res.ok) throw new Error("Priority search failed");
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setResults(data.results || []);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [JSON.stringify(filters)]);

  if (loading) return <div className="pjl-status">Loading jobs\u2026</div>;
  if (error) return <div className="pjl-status pjl-status--error">{error}</div>;
  if (!results.length) return <div className="pjl-status">No matching jobs found.</div>;

  return (
    <div className="pjl-list">
      {results.map((r) => (
        <div key={r.job.job_id || r.job._id} className="pjl-row">
          {renderJob(r.job, r.badge)}
        </div>
      ))}
    </div>
  );
}
