---
name: dashboard-domain-specification
description: A reusable template for defining detailed domain specifications for dashboard projects, including panel requirements, data sources, and interfaces.
---

# Dashboard Domain Specification

This skill provides a structured approach to creating detailed domain specifications for dashboard projects. It ensures clarity and completeness by covering panel requirements, data sources, and interfaces.

## Steps

1. **Define Dashboard Purpose**:
   - Clearly state the primary goal of the dashboard (e.g., monitoring, analytics, reporting).

2. **List Panels**:
   - Enumerate all panels to be included in the dashboard.
   - For each panel, specify:
     - **Title**: Descriptive name of the panel.
     - **Purpose**: What the panel aims to display or achieve.
     - **Data Sources**: Where the data for the panel will come from (e.g., APIs, databases).
     - **Refresh Rate**: How often the data should be updated (e.g., real-time, hourly).

3. **Specify Data Sources**:
   - Detail each data source, including:
     - **Type**: API, database, file, etc.
     - **Endpoint/Path**: URL or path to access the data.
     - **Authentication**: Any required credentials or tokens.
     - **Data Format**: JSON, CSV, etc.

4. **Define Interfaces**:
   - Describe the interfaces for interacting with the dashboard:
     - **User Interface**: Layout, navigation, and interactivity.
     - **Programmatic Interface**: APIs or hooks for external systems.

5. **Include Examples**:
   - Provide sample configurations or code snippets for common panels (e.g., time-series charts, tables).

## Example

### Panel Specification
```yaml
- title: "Daily Active Users"
  purpose: "Display the number of active users per day"
  data_sources:
    - type: "API"
      endpoint: "https://api.example.com/users/daily"
      authentication: "Bearer token"
      data_format: "JSON"
  refresh_rate: "hourly"
```

### Data Source Specification
```yaml
- name: "User Activity API"
  type: "REST API"
  endpoint: "https://api.example.com/users"
  authentication: "OAuth 2.0"
  data_format: "JSON"
```

## Best Practices

- **Modularity**: Keep panel specifications modular for easy updates.
- **Consistency**: Use consistent naming conventions for panels and data sources.
- **Documentation**: Maintain detailed documentation for all components