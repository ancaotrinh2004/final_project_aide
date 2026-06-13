# 📜 Loki Setup

> Deploy **Loki + Promtail** (loki-stack) for centralized log aggregation — query structured JSON logs across the inference service and Airflow pipelines from Grafana via LogQL.

<table>
<tr><th>Component</th><th>Version</th><th>Helm Chart</th></tr>
<tr><td>Loki</td><td><b>2.9.3</b></td><td><code>grafana/loki-stack</code></td></tr>
<tr><td>Promtail</td><td>bundled</td><td><code>grafana/loki-stack</code></td></tr>
<tr><td>Grafana</td><td>reused</td><td>subchart disabled</td></tr>
</table>

> [!NOTE]
> **Prerequisites:** the [monitoring stack](monitoring-setup.md) must be deployed — Loki reuses the existing `kube-prom-grafana` as its UI, so the Grafana subchart is disabled.

---

## 🚀 Setup

### 1. Deploy Loki + Promtail

```bash
helm repo add grafana https://grafana.github.io/helm-charts && helm repo update
helm install loki grafana/loki-stack --namespace monitoring --values infra/helm/loki/values.yaml
```

```bash
kubectl get pods -n monitoring | grep -E "loki|promtail"
# loki-0              1/1  Running
# loki-promtail-xxx   1/1  Running   (DaemonSet)
```

> [!WARNING]
> **Knative appends the revision to the `app` label** — the predictor pod is `app=fraud-predictor-00001`, not `fraud-predictor`. So Promtail's `keep` relabel must match the prefix **`fraud-predictor.*`**; an exact match silently drops all inference logs (you'd only see `airflow` logs). Already fixed in `values.yaml`.

Confirm both namespaces are ingested:

```bash
kubectl run loki-q --rm -i --restart=Never --image=curlimages/curl -n monitoring -q -- \
  -s "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/label/namespace/values"
# {"status":"success","data":["airflow","fraud-infra"]}
```

### 2. Loki data source (auto-provision + the default-clash fix)

loki-stack auto-creates the Loki data source ConfigMap (`grafana_datasource=1`) — **but** marks it `isDefault: true`, which collides with Prometheus and crash-loops any new Grafana pod (*"Only one datasource per organization can be marked as default"*).

> [!IMPORTANT]
> loki-stack exposes no value to disable this. Patch the ConfigMap to `isDefault: false`, then restart Grafana. **This patch is overwritten by `helm upgrade loki`** — re-apply it after any upgrade.

```bash
kubectl patch configmap loki-loki-stack -n monitoring --type merge -p \
  '{"data":{"loki-stack-datasource.yaml":"apiVersion: 1\ndatasources:\n- name: Loki\n  type: loki\n  access: proxy\n  url: \"http://loki:3100\"\n  version: 1\n  isDefault: false\n  jsonData:\n    {}\n"}}'
kubectl rollout restart deployment/kube-prom-grafana -n monitoring
```

> [!TIP]
> Grafana has long probe delays (readiness 60s, liveness 180s) — wait ~2 min before judging a restart failed.

---

## ✅ Verify — Query logs (LogQL)

Grafana → **Explore** → data source **Loki**:

```logql
{namespace="fraud-infra"} | json | level="INFO"      # inference server logs
{namespace="fraud-infra"} | json | fraud_score > 0.8 # high-risk predictions
{namespace="airflow"}     | json | level="ERROR"     # pipeline errors
```

Structured JSON logs expose `request_id`, `customer_id`, `fraud_score`, `latency_ms`, `n_instances`.

<p align="center">
  <img src="assets/loki_logging.png" width="820" alt="Loki structured logs in Grafana Explore"/>
  <br/><em>Inference & pipeline logs aggregated in Loki, queried via LogQL in Grafana Explore.</em>
</p>

---

## 🧹 Teardown

```bash
helm uninstall loki --namespace monitoring
```
