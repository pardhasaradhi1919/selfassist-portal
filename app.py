"""Self-service stack portal.

Predefined stacks -> download template zip (from git template branch) and/or trigger Jenkins.
Custom stack      -> upload stack zip (+ optional Dockerfile) -> Kaniko build/push -> apply k8s manifests.
"""
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from pathlib import Path

import requests
import streamlit as st
import yaml
from kubernetes import client, config as kconfig, dynamic
from kubernetes.client.rest import ApiException

# ----------------------------------------------------------------------------- settings
E = os.getenv
JENKINS_URL = E("JENKINS_URL", "").rstrip("/")
JENKINS_JOB = E("JENKINS_JOB", "stack-provisioner")          # use "folder/pipeline" for foldered jobs
JENKINS_USER, JENKINS_TOKEN = E("JENKINS_USER", ""), E("JENKINS_TOKEN", "")
JENKINS_VERIFY_TLS = E("JENKINS_VERIFY_TLS", "true").lower() == "true"

GIT_REPO_URL = E("GIT_REPO_URL", "")                          # https://git.example.com/org/templates.git
GIT_USER, GIT_TOKEN = E("GIT_USER", "oauth2"), E("GIT_TOKEN", "")
TEMPLATE_BRANCH = E("TEMPLATE_BRANCH", "stack-templates")
TEMPLATES_DIR = E("TEMPLATES_DIR", "templates")               # templates/<stack-folder>/...

REGISTRY = E("REGISTRY", "").rstrip("/")                      # e.g. myacr.azurecr.io/selfservice
REGISTRY_SECRET = E("REGISTRY_SECRET", "regcred")             # dockerconfigjson secret in build namespace
KANIKO_IMAGE = E("KANIKO_IMAGE", "gcr.io/kaniko-project/executor:v1.23.2")
BUILD_TIMEOUT = int(E("BUILD_TIMEOUT_SECONDS", "900"))
CONTEXT_ROOT = Path(E("CONTEXT_ROOT", "/contexts"))            # PVC mounted in the portal pod
CONTEXT_PVC = E("CONTEXT_PVC", "selfservice-contexts")        # same PVC, mounted into Kaniko Jobs
MAX_ZIP_MB = int(E("MAX_ZIP_MB", "50"))
MAX_UNZIPPED_MB = int(E("MAX_UNZIPPED_MB", "200"))
MAX_FILES = int(E("MAX_FILES", "5000"))
MAX_REPLICAS = int(E("MAX_REPLICAS", "5"))
ALLOWED_KINDS = {k.strip() for k in E(
    "ALLOWED_KINDS", "Deployment,StatefulSet,Service,ConfigMap,Ingress,"
                     "HorizontalPodAutoscaler,PersistentVolumeClaim,CronJob").split(",") if k.strip()}
MANIFEST_DIRS = ("k8s", "manifests", "deploy", "deployment")  # folders inside the zip scanned for YAML
KIND_ORDER = ["ConfigMap", "PersistentVolumeClaim", "Service", "Deployment", "StatefulSet",
              "CronJob", "Ingress", "HorizontalPodAutoscaler"]
CREATE_NAMESPACE = E("CREATE_NAMESPACE", "true").lower() == "true"
ALLOWED_NS_REGEX = E("ALLOWED_NS_REGEX", r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
BLOCKED_NS = {"kube-system", "kube-public", "kube-node-lease", "default"}

_NS_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
BUILD_NS = E("BUILD_NAMESPACE") or (Path(_NS_FILE).read_text().strip() if Path(_NS_FILE).exists() else "default")

DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")

# key, label, icon, description, template folder
STACKS = [
    ("react-fastapi", "React + FastAPI", "⚛️", "SPA frontend with async Python API", "react-fastapi"),
    ("react-flask", "React + Flask", "🧪", "SPA frontend with lightweight Python API", "react-flask"),
    ("react-dotnet", "React + C#", "🟣", "SPA frontend with ASP.NET Core API", "react-dotnet"),
    ("react-django", "React + Django", "🎸", "SPA frontend with Django REST", "react-django"),
    ("react-springboot", "React + Spring Boot", "🍃", "SPA frontend with Java Spring Boot", "react-springboot"),
    ("custom", "Custom", "🛠️", "Upload your own Dockerfile + config", None),
]


# ----------------------------------------------------------------------------- helpers
def validate_namespace(ns: str):
    if not ns:
        return "Target namespace is required."
    if not DNS1123.match(ns):
        return "Use lowercase letters, digits and '-' (max 63 chars)."
    if ns in BLOCKED_NS:
        return f"'{ns}' is not allowed."
    if not re.match(ALLOWED_NS_REGEX, ns):
        return f"Namespace must match policy: {ALLOWED_NS_REGEX}"
    return None


def requester() -> str:
    try:
        h = st.context.headers
        return h.get("X-Forwarded-Email") or h.get("X-Forwarded-User") or "unknown"
    except Exception:
        return "unknown"


def _auth_url(url: str) -> str:
    if GIT_TOKEN and url.startswith("https://"):
        return url.replace("https://", f"https://{GIT_USER}:{GIT_TOKEN}@", 1)
    return url


def build_stack_zip(folder: str, ns: str) -> bytes:
    """Shallow-clone the template branch and zip templates/<folder>, replacing {{TARGET_NAMESPACE}}."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp, "repo")
        r = subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", TEMPLATE_BRANCH, _auth_url(GIT_REPO_URL), str(repo)],
            capture_output=True, text=True, timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        if r.returncode:
            err = r.stderr.replace(GIT_TOKEN, "***") if GIT_TOKEN else r.stderr
            raise RuntimeError(f"git clone failed: {err.strip()}")
        src = repo / TEMPLATES_DIR / folder
        if not src.is_dir():
            raise RuntimeError(f"'{TEMPLATES_DIR}/{folder}' not found on branch '{TEMPLATE_BRANCH}'.")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in src.rglob("*"):
                if p.is_file() and ".git" not in p.parts:
                    data = p.read_bytes()
                    try:
                        data = data.decode().replace("{{TARGET_NAMESPACE}}", ns).encode()
                    except UnicodeDecodeError:
                        pass
                    z.writestr(f"{folder}/{p.relative_to(src)}", data)
        return buf.getvalue()


def trigger_jenkins(stack: str, ns: str):
    path = "/".join(f"job/{p}" for p in JENKINS_JOB.split("/"))
    r = requests.post(f"{JENKINS_URL}/{path}/buildWithParameters",
                      auth=(JENKINS_USER, JENKINS_TOKEN), verify=JENKINS_VERIFY_TLS, timeout=30,
                      data={"STACK": stack, "TARGET_NAMESPACE": ns,
                            "TEMPLATE_BRANCH": TEMPLATE_BRANCH, "REQUESTED_BY": requester()})
    r.raise_for_status()
    queue = r.headers.get("Location", "")
    build_url = None
    for _ in range(15):                                        # try to resolve queue item -> build URL
        try:
            j = requests.get(f"{queue}api/json", auth=(JENKINS_USER, JENKINS_TOKEN),
                             verify=JENKINS_VERIFY_TLS, timeout=10).json()
            if j.get("executable"):
                build_url = j["executable"]["url"]
                break
        except Exception:
            pass
        time.sleep(2)
    return queue, build_url


@st.cache_resource
def k8s():
    try:
        kconfig.load_incluster_config()
    except kconfig.ConfigException:
        kconfig.load_kube_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.BatchV1Api()


def upsert(create, patch, name, ns, body):
    try:
        create(ns, body)
    except ApiException as e:
        if e.status != 409:
            raise
        patch(name, ns, body)


@st.cache_resource
def dyn_client():
    k8s()  # ensures kube config is loaded
    return dynamic.DynamicClient(client.ApiClient())


def extract_zip(data: bytes, dest: Path):
    """Safe extraction: no path traversal, no symlinks, size/file-count caps. Strips one wrapper folder."""
    limit = MAX_UNZIPPED_MB * 1024 * 1024
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos = [i for i in z.infolist() if not i.filename.startswith("__MACOSX")]
        if len(infos) > MAX_FILES:
            raise ValueError(f"Zip has more than {MAX_FILES} entries.")
        dest.mkdir(parents=True)
        root = dest.resolve()
        for i in infos:
            target = (dest / i.filename).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"Unsafe path in zip: {i.filename}")
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Symlinks are not allowed: {i.filename}")
            if i.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"Unzipped size exceeds {MAX_UNZIPPED_MB} MB.")
                    out.write(chunk)
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():              # my-stack/Dockerfile -> Dockerfile
        top = entries[0].rename(dest / ".top")
        for child in top.iterdir():
            shutil.move(str(child), str(dest / child.name))
        top.rmdir()


def cleanup_stale(hours: int = 6):
    if CONTEXT_ROOT.is_dir():
        for d in CONTEXT_ROOT.iterdir():
            if d.is_dir() and time.time() - d.stat().st_mtime > hours * 3600:
                shutil.rmtree(d, ignore_errors=True)


def _pod_spec(doc: dict):
    spec, kind = doc.get("spec") or {}, doc.get("kind")
    if kind in ("Deployment", "StatefulSet"):
        return (spec.get("template") or {}).get("spec")
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    return None


def inject_pull_secret(doc: dict):
    ps = _pod_spec(doc)
    if ps is not None:
        refs = ps.setdefault("imagePullSecrets", [])
        if not any(r.get("name") == REGISTRY_SECRET for r in refs):
            refs.append({"name": REGISTRY_SECRET})


def load_manifests(ctx: Path, ns: str, image: str):
    """Read YAML from <zip>/k8s|manifests|deploy|deployment. Placeholders: {{IMAGE}}, {{TARGET_NAMESPACE}}."""
    mdir = next((p for p in ctx.iterdir() if p.is_dir() and p.name.lower() in MANIFEST_DIRS), None)
    if not mdir:
        return [], False
    objs, used_image = [], False
    for f in sorted([*mdir.rglob("*.yaml"), *mdir.rglob("*.yml")]):
        text = f.read_text()
        used_image = used_image or "{{IMAGE}}" in text
        text = text.replace("{{IMAGE}}", image).replace("{{TARGET_NAMESPACE}}", ns)
        for doc in yaml.safe_load_all(text):
            if not doc:
                continue
            if not isinstance(doc, dict) or not doc.get("kind") or not doc.get("apiVersion"):
                raise ValueError(f"{f.name}: every document needs apiVersion and kind.")
            if doc["kind"] not in ALLOWED_KINDS:
                raise ValueError(f"{f.name}: kind '{doc['kind']}' is not allowed. "
                                 f"Allowed: {', '.join(sorted(ALLOWED_KINDS))}")
            meta = doc.setdefault("metadata", {})
            if not meta.get("name"):
                raise ValueError(f"{f.name}: {doc['kind']} is missing metadata.name.")
            meta["namespace"] = ns                              # always force the target namespace
            inject_pull_secret(doc)
            objs.append(doc)
    return objs, used_image


def default_manifests(name: str, port: int, replicas: int, image: str, ns: str):
    labels = {"app": name, "managed-by": "selfservice-portal"}
    dep = {"apiVersion": "apps/v1", "kind": "Deployment",
           "metadata": {"name": name, "namespace": ns, "labels": labels},
           "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": name}},
                    "template": {"metadata": {"labels": labels},
                                 "spec": {"containers": [{"name": name, "image": image,
                                                          "ports": [{"containerPort": port}]}]}}}}
    svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns, "labels": labels},
           "spec": {"selector": {"app": name}, "ports": [{"port": port, "targetPort": port}]}}
    inject_pull_secret(dep)
    return [dep, svc]


def _build_logs(job: str) -> str:
    core, _, _ = k8s()
    try:
        pods = core.list_namespaced_pod(BUILD_NS, label_selector=f"job-name={job}").items
        return core.read_namespaced_pod_log(pods[0].metadata.name, BUILD_NS, container="kaniko") if pods else ""
    except ApiException:
        return ""


def run_build(rid: str, image: str, log_box) -> bool:
    """Kaniko Job mounting the shared PVC (subPath=<rid>) as build context. Pinned to the portal's node."""
    _, _, batch = k8s()
    job = f"build-{rid}"
    batch.create_namespaced_job(BUILD_NS, {
        "metadata": {"name": job, "labels": {"app": "selfservice-build"}},
        "spec": {"backoffLimit": 0, "ttlSecondsAfterFinished": 3600,
                 "template": {"spec": {
                     "restartPolicy": "Never",
                     "affinity": {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [{
                         "labelSelector": {"matchLabels": {"app": "selfservice-portal"}},
                         "topologyKey": "kubernetes.io/hostname"}]}},     # lets a RWO PVC be shared
                     "containers": [{
                         "name": "kaniko", "image": KANIKO_IMAGE,
                         "args": ["--dockerfile=/workspace/Dockerfile", "--context=dir:///workspace",
                                  f"--destination={image}", "--cache=false"],
                         "volumeMounts": [{"name": "ctx", "mountPath": "/workspace", "subPath": rid, "readOnly": True},
                                          {"name": "docker", "mountPath": "/kaniko/.docker"}],
                         "resources": {"requests": {"cpu": "500m", "memory": "1Gi"},
                                       "limits": {"cpu": "2", "memory": "4Gi"}}}],
                     "volumes": [{"name": "ctx", "persistentVolumeClaim": {"claimName": CONTEXT_PVC}},
                                 {"name": "docker", "secret": {"secretName": REGISTRY_SECRET,
                                                               "items": [{"key": ".dockerconfigjson",
                                                                          "path": "config.json"}]}}]}}}})
    deadline = time.time() + BUILD_TIMEOUT
    while time.time() < deadline:
        s = batch.read_namespaced_job_status(job, BUILD_NS).status
        log_box.code((_build_logs(job) or "waiting for builder pod...")[-4000:])
        if s.succeeded:
            return True
        if s.failed:
            return False
        time.sleep(3)
    batch.delete_namespaced_job(job, BUILD_NS, propagation_policy="Background")
    return False


def prepare_namespace(ns: str):
    core, _, _ = k8s()
    if CREATE_NAMESPACE:
        try:
            core.create_namespace({"metadata": {"name": ns, "labels": {
                "managed-by": "selfservice-portal", "pod-security.kubernetes.io/enforce": "baseline"}}})
        except ApiException as e:
            if e.status != 409:
                raise
    src = core.read_namespaced_secret(REGISTRY_SECRET, BUILD_NS)   # copy pull secret to target ns
    upsert(core.create_namespaced_secret, core.patch_namespaced_secret, REGISTRY_SECRET, ns,
           {"metadata": {"name": REGISTRY_SECRET}, "type": src.type, "data": src.data})


def apply_objects(objs: list, ns: str) -> list:
    dyn, done = dyn_client(), []
    for o in sorted(objs, key=lambda o: KIND_ORDER.index(o["kind"]) if o["kind"] in KIND_ORDER else 99):
        res = dyn.resources.get(api_version=o["apiVersion"], kind=o["kind"])
        if not res.namespaced:
            raise ValueError(f"{o['kind']} is cluster-scoped and not allowed.")
        name = o["metadata"]["name"]
        dyn.server_side_apply(res, body=o, name=name, namespace=ns,
                              field_manager="selfservice-portal", force_conflicts=True)
        done.append(f"{o['kind']}/{name}")
    return done


def wait_rollouts(objs: list, ns: str) -> list:
    """Returns names of Deployments still not ready after ~5 minutes."""
    _, apps, _ = k8s()
    pending = [o["metadata"]["name"] for o in objs if o["kind"] == "Deployment"]
    for _ in range(60):
        still = []
        for n in pending:
            d = apps.read_namespaced_deployment_status(n, ns)
            want = d.spec.replicas if d.spec.replicas is not None else 1
            if not ((d.status.ready_replicas or 0) >= want and (d.status.updated_replicas or 0) >= want):
                still.append(n)
        pending = still
        if not pending:
            break
        time.sleep(5)
    return pending


def run_custom(zip_bytes: bytes, dockerfile: bytes | None, app: str, port: int, replicas: int, ns: str):
    rid = uuid.uuid4().hex[:8]
    ctx = CONTEXT_ROOT / rid
    image = f"{REGISTRY}/{ns}-{app}:{time.strftime('%Y%m%d%H%M%S')}"
    with st.status("Unpacking stack...", expanded=True) as status:
        try:
            cleanup_stale()
            extract_zip(zip_bytes, ctx)
            if dockerfile:
                (ctx / "Dockerfile").write_bytes(dockerfile)
            if not (ctx / "Dockerfile").is_file():
                raise ValueError("No Dockerfile: upload one or put it at the zip root.")
            objs, used_image = load_manifests(ctx, ns, image)
            if not objs:
                objs = default_manifests(app, port, replicas, image, ns)
                st.info("No k8s/ manifests found in the zip — using a default Deployment + Service.")
            elif not used_image:
                st.warning("Manifests don't reference {{IMAGE}}, so the image just built won't be used.")
            st.write("Will apply: " + ", ".join(f"{o['kind']}/{o['metadata']['name']}" for o in objs))

            status.update(label="Building image...")
            log_box = st.empty()
            if not run_build(rid, image, log_box):
                status.update(label="Build failed", state="error")
                return
            st.write(f"Image pushed: `{image}`")

            status.update(label=f"Deploying to {ns}...")
            prepare_namespace(ns)
            applied = apply_objects(objs, ns)
            pending = wait_rollouts(objs, ns)
            if pending:
                status.update(label="Rollout timed out", state="error")
                st.error(f"Not ready yet: {', '.join(pending)}")
                return
            status.update(label="Deployed ✅", state="complete")
            st.success(f"Applied {len(applied)} resource(s) to `{ns}`.")
            st.code("\n".join(applied) + f"\n\nkubectl -n {ns} get all", language="bash")
        except Exception as e:
            status.update(label="Error", state="error")
            st.error(str(e))
        finally:
            shutil.rmtree(ctx, ignore_errors=True)


# ----------------------------------------------------------------------------- UI
st.set_page_config(page_title="Self-Service Stacks", page_icon="🚀", layout="wide")
st.title("🚀 Self-Service Stack Portal")
st.caption("Pick a stack, choose a target namespace, and go.")

st.subheader("1 · Choose a stack")
cols = st.columns(3)
for i, (key, label, icon, desc, _) in enumerate(STACKS):
    with cols[i % 3]:
        with st.container(border=True):
            st.markdown(f"### {icon} {label}")
            st.caption(desc)
            sel = st.session_state.get("stack") == key
            if st.button("✅ Selected" if sel else "Select", key=f"tile-{key}",
                         type="primary" if sel else "secondary", use_container_width=True):
                st.session_state["stack"] = key
                st.rerun()

stack = st.session_state.get("stack")
if not stack:
    st.stop()
meta = next(s for s in STACKS if s[0] == stack)

st.divider()
st.subheader(f"2 · Configure — {meta[1]}")
ns = st.text_input("Target namespace", placeholder="team-a-dev").strip()
ns_err = validate_namespace(ns) if ns else None
if ns_err:
    st.error(ns_err)
ready = bool(ns) and not ns_err

# ---- predefined stacks -------------------------------------------------------
if stack != "custom":
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Download template**")
        zkey = f"zip::{stack}::{ns}"
        if st.button("Prepare download", disabled=not ready or not GIT_REPO_URL):
            try:
                with st.spinner("Fetching template from git..."):
                    st.session_state[zkey] = build_stack_zip(meta[4], ns)
            except Exception as e:
                st.error(str(e))
        if zkey in st.session_state:
            st.download_button("⬇️ Download stack (.zip)", st.session_state[zkey],
                               file_name=f"{stack}-{ns}.zip", mime="application/zip")
        if not GIT_REPO_URL:
            st.caption("GIT_REPO_URL is not configured.")
    with c2:
        st.markdown("**Provision via Jenkins**")
        if st.button("Trigger pipeline", type="primary", disabled=not ready or not JENKINS_URL):
            try:
                queue, build_url = trigger_jenkins(stack, ns)
                st.success("Pipeline triggered.")
                st.markdown(f"[Open build]({build_url})" if build_url else f"Queued: {queue}")
            except Exception as e:
                st.error(f"Jenkins call failed: {e}")
        if not JENKINS_URL:
            st.caption("JENKINS_URL is not configured.")

# ---- custom stack ------------------------------------------------------------
else:
    st.markdown("Upload a **zip of your entire stack**: source for the image build plus your Kubernetes manifests.")
    st.code("""my-stack.zip
├── Dockerfile              (optional here if you upload it separately below)
├── src/ ...                (anything the Dockerfile COPYs)
└── k8s/                    (or manifests/, deploy/, deployment/)
    ├── deployment.yaml     (use image: {{IMAGE}} and namespace: {{TARGET_NAMESPACE}})
    └── service.yaml""", language="text")
    u1, u2 = st.columns(2)
    zip_file = u1.file_uploader("Stack zip", type=["zip"])
    df_file = u2.file_uploader("Dockerfile (optional, overrides the one in the zip)")

    with st.expander("Fallback settings (used only if the zip has no k8s/ manifests)"):
        f1, f2, f3 = st.columns(3)
        app = f1.text_input("App / image name", value="custom-app").strip()
        port = int(f2.number_input("Port", 1, 65535, 8080))
        replicas = int(f3.number_input("Replicas", 1, MAX_REPLICAS, 1))

    problem = None
    if zip_file:
        if zip_file.size > MAX_ZIP_MB * 1024 * 1024:
            problem = f"Zip is larger than {MAX_ZIP_MB} MB."
        else:
            try:
                names = zipfile.ZipFile(io.BytesIO(zip_file.getvalue())).namelist()
                with st.expander(f"Zip contents ({len(names)} entries)"):
                    st.code("\n".join(names[:60]) + ("\n..." if len(names) > 60 else ""))
            except zipfile.BadZipFile:
                problem = "That file is not a valid zip."
    if not DNS1123.match(app):
        problem = "App name must be a valid DNS label (lowercase, digits, '-')."
    if problem:
        st.error(problem)

    if st.button("🚀 Build & Deploy", type="primary",
                 disabled=not (ready and zip_file and REGISTRY and not problem)):
        run_custom(zip_file.getvalue(), df_file.getvalue() if df_file else None, app, port, replicas, ns)
    if not REGISTRY:
        st.caption("REGISTRY is not configured.")
