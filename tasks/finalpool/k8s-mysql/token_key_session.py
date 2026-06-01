from addict import Dict
import os

print("Load token key session")
# I am gradually modifying the tokens to the pseudo account in this project

file_path = os.path.abspath(__file__)


def _instance_suffix() -> str:
    """Read instance_suffix from configs/ports_config.yaml so the kubeconfig
    filename matches what the preprocess shell script wrote (per-instance
    suffixed cluster name, to avoid cross-instance kind collisions).  Empty
    string when running on a single-instance setup or if the field is
    missing — backward-compatible with the original ``cluster-mysql-config.yaml``.
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
    f"cluster-mysql{_instance_suffix()}-config.yaml",
)

all_token_key_session = Dict(
    # k8s
    kubeconfig_path = kubeconfig_path,
)