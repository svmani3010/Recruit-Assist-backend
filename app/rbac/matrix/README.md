# Permission matrix (source of truth)

This folder holds the **exact** Recruitment role catalogue and the **Yes / No**
action grid used by the API.

| File | Purpose |
|------|---------|
| `roles.json` | Seven roles from the permission sheet (labels, designations, notes) |
| `recruitment_actions.json` | Action × role grid — values are only `yes` or `no` |
| `loader.py` | Loads JSON and seeds Mongo `permission_matrix` |

## Rules

1. **Only `yes` or `no`.** Former Full / Team / Own / Assigned / Config → `yes`.
2. If an action is missing for a role → treat as **`no`**.
3. API requests resolve: session → `organization_id` + `user_id` → role → this matrix.
4. Denied calls return: *You don't have access to this action. Contact a Super Admin, Content Admin, or Manager.*

Core website UI is unchanged; enforcement is on the API.
