from addict import Dict
import os
# I am gradually modifying the tokens to the pseudo account in this project

# find theabs path of this file
file_path = os.path.abspath(__file__)

emails_config_file = os.path.join(os.path.dirname(file_path), "emails_config.json")


def _instance_suffix() -> str:
    """Read instance_suffix from configs/ports_config.yaml so the kubeconfig
    filename matches what the preprocess shell script wrote (per-instance
    suffixed cluster name, to avoid cross-instance kind collisions).  Empty
    string when running on a single-instance setup — backward-compatible
    with the original ``cluster-cleanup-config.yaml``.
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
    f"cluster-cleanup{_instance_suffix()}-config.yaml",
)

all_token_key_session = Dict(
    # poste emails
    emails_config_file = emails_config_file,
    # k8s
    kubeconfig_path = kubeconfig_path,
)