import React, { useState } from "react";
import "./SubscriptionPlans.css";

const PLANS = [
  {
    tier: "enterprise",
    name: "Enterprise",
    tagline: "Top-of-page visibility",
    points: [
      "Guaranteed positions 1–2 on every matching search",
      "Fair-share rotation across other Enterprise jobs",
      "\u201cPromoted\u201d badge on every listing",
    ],
  },
  {
    tier: "premium",
    name: "Premium",
    tagline: "Priority placement",
    points: [
      "Guaranteed positions 3–10 on every matching search",
      "Fair-share rotation across other Premium jobs",
      "\u201cPriority listing\u201d badge on every listing",
    ],
  },
  {
    tier: "standard",
    name: "Standard",
    tagline: "Organic reach",
    points: [
      "Included with every job post, no extra step",
      "Ranked purely by relevance and recency",
      "No promoted badge",
    ],
  },
];

async function submitSubscription(tier) {
  const res = await fetch("/api/subscriptions/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ tier }),
  });
  if (!res.ok) {
    throw new Error("Subscription request failed");
  }
  return res.json();
}

export default function SubscriptionPlans({ currentTier = "standard" }) {
  const [pendingTier, setPendingTier] = useState(null);
  const [confirmedTier, setConfirmedTier] = useState(null);
  const [error, setError] = useState(null);

  async function handleSubscribe(tier) {
    setError(null);
    setPendingTier(tier);
    try {
      await submitSubscription(tier);
      setConfirmedTier(tier);
    } catch (err) {
      setError("Couldn't submit your request. Please try again.");
    } finally {
      setPendingTier(null);
    }
  }

  return (
    <div className="subplan-wrap">
      <div className="subplan-head">
        <h2 className="subplan-title">Priority Listing Plans</h2>
        <p className="subplan-sub">
          Choose how your job postings are ranked in candidate search results.
          Requests are reviewed by an admin before priority placement goes live.
        </p>
      </div>

      {error && <p className="subplan-error">{error}</p>}

      <div className="subplan-grid">
        {PLANS.map((plan) => {
          const isCurrent = currentTier === plan.tier;
          const isPending = pendingTier === plan.tier;
          const isConfirmed = confirmedTier === plan.tier;
          const isStandard = plan.tier === "standard";

          return (
            <div
              key={plan.tier}
              className={`subplan-card${plan.tier === "enterprise" ? " is-featured" : ""}`}
            >
              {plan.tier === "enterprise" && (
                <span className="subplan-ribbon">Best visibility</span>
              )}
              <h3 className="subplan-card-name">{plan.name}</h3>
              <p className="subplan-card-tagline">{plan.tagline}</p>

              <ul className="subplan-points">
                {plan.points.map((point) => (
                  <li key={point}>{point}</li>
                ))}
              </ul>

              {isCurrent ? (
                <button type="button" className="subplan-btn subplan-btn--current" disabled>
                  Current plan
                </button>
              ) : isConfirmed ? (
                <button type="button" className="subplan-btn subplan-btn--pending" disabled>
                  Request sent &mdash; awaiting admin approval
                </button>
              ) : (
                <button
                  type="button"
                  className="subplan-btn"
                  disabled={isStandard || isPending}
                  onClick={() => handleSubscribe(plan.tier)}
                >
                  {isPending ? "Submitting\u2026" : isStandard ? "Included" : `Subscribe to ${plan.name}`}
                </button>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
