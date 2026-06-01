from addict import Dict
import os

file_path = os.path.abspath(__file__)

folder_id_file = os.path.join(os.path.dirname(file_path), "files", "folder_id.txt")
with open(folder_id_file, "r") as f:
    folder_id = f.read().strip()


def _instance_suffix() -> str:
    """Read instance_suffix from configs/ports_config.yaml so the kubeconfig
    filename matches what the preprocess shell script wrote.  Empty when
    running on a single-instance setup — backward-compatible with the
    original ``cluster-safety-audit-config.yaml``.
    """
    cfg_path = os.path.join(
        os.path.dirname(file_path), "..", "..", "..", "configs", "ports_config.yaml"
    )
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("instance_suffix:"):
                    val = line.split(":", 1)[1].strip()
                    return val.strip('"').strip("'")
    except OSError:
        pass
    return ""


kubeconfig_path = os.path.join(
    os.path.dirname(file_path),
    "k8s_configs",
    f"cluster-safety-audit{_instance_suffix()}-config.yaml",
)

all_token_key_session = Dict(
    google_sheets_folder_id = folder_id,
    # k8s
    kubeconfig_path = kubeconfig_path,
)