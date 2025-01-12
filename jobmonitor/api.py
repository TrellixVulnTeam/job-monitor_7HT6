import datetime
import os
import random
import string
import tarfile
from collections import namedtuple
from collections.abc import Iterable
from fnmatch import fnmatch
from tempfile import NamedTemporaryFile
from typing import List

from bson.objectid import ObjectId
from git import InvalidGitRepositoryError, Repo
from schema import Or, Schema

from jobmonitor.lazy_loader import LazyLoader

kubernetes_client = LazyLoader("k", globals(), "kubernetes.client")
kubernetes = LazyLoader("kubernetes", globals(), "kubernetes")
KUBERNETES_NAMESPACE = LazyLoader(
    "KUBERNETES_NAMESPACE", globals(), "jobmonitor.connections.KUBERNETES_NAMESPACE"
)
c = LazyLoader("c", globals(), "jobmonitor.connections")


def job_by_id(job_id):
    return c.mongo.job.find_one(
        {"_id": ObjectId(job_id)}, {"logs": 0, "metric_data": 0, "workers": 0, "metrics": 0}
    )


def delete_job_by_id(job_id):
    return c.mongo.job.delete_one({"_id": ObjectId(job_id)})


def update_job(job_id, update_dict, w=None):
    """w: mongodb writeConcern, use w=0 to continue without acknowledgement"""
    return c.mongo.job.update({"_id": ObjectId(job_id)}, {"$set": update_dict}, w=w)


def register_job(
    project,
    experiment,
    job,
    config_overrides,
    runtime_environment,
    annotations=None,
    user=None,
    n_workers=1,
    priority=1,
):
    if user is None:
        user = os.getenv("USER")

    # Validate the inputs
    Schema(str).validate(user)
    Schema(str).validate(project)
    Schema(str).validate(experiment)
    Schema(str).validate(job)
    Schema(Or({}, {str: object})).validate(config_overrides)
    Schema(Or(None, {str: object})).validate(annotations)
    Schema({"clone": Or({"code_package": ObjectId}, {"path": str}), "script": str}).validate(
        runtime_environment
    )

    # Format the database entry
    job_content = {
        "user": user,
        "project": project,
        "experiment": experiment,
        "job": job,
        "n_workers": n_workers,
        "registered_workers": 0,
        "config": config_overrides,
        "environment": runtime_environment,
        "status": "CREATED",
        "creation_time": datetime.datetime.utcnow(),
        "priority": priority,
    }
    if annotations is not None:
        job_content["annotations"] = annotations

    insert_result = c.mongo.job.insert_one(job_content)
    job_id = str(insert_result.inserted_id)
    return job_id


def kubernetes_delete_job(kubernetes_job_name):
    kubernetes.config.load_kube_config()
    client = kubernetes_client.BatchV1Api()
    body = kubernetes_client.V1DeleteOptions(propagation_policy="Foreground")
    return client.delete_namespaced_job(
        kubernetes_job_name, namespace=c.KUBERNETES_NAMESPACE, body=body
    )


def kubernetes_schedule_job(
    job_id,
    docker_image_path,
    volumes,
    gpus=0,
    environment_variables={},
    results_dir="/scratch/results",
):
    """
    Example inputs:
    docker_image_path: ic-registry.epfl.ch/mlo/jobmonitor_worker
    volumes: ['pv-mlodata1'] or {'pv-mlodata1': '/mlodata1'}
    """
    if not isinstance(volumes, Iterable):
        raise ValueError(
            "Volumes should either be an iterable (list, tuple) or a dictionary {'volume_name': 'mount_path'}."
        )
    if not isinstance(volumes, dict):
        volumes = {volume: "/" + volume for volume in volumes}  # use volume-name as mount-path

    job = job_by_id(job_id)
    kubernetes.config.load_kube_config()
    client = kubernetes_client.Corek.V1Api()
    pod_name = "{}-{}".format(job["user"], job_id[-6:])
    metadata = k.V1ObjectMeta(
        name=pod_name,
        labels=dict(
            app="jobmonitor",
            user=job["user"],
            project=job["project"],
            experiment=job["experiment"],
            job=job["job"],
            job_id=job_id,
        ),
    )
    pod_spec = kubernetes_create_base_pod_spec(
        cmd=["jobrun", job_id],
        docker_image_path=docker_image_path,
        gpus=gpus,
        environment_variables={"JOBMONITOR_RESULTS_DIR": results_dir, **environment_variables},
        volumes=volumes,
    )
    pod = k.V1Pod(metadata=metadata, spec=pod_spec)
    client.create_namespaced_pod(c.KUBERNETES_NAMESPACE, pod)
    update_job(job_id, {"status": "SCHEDULED", "schedule_time": datetime.datetime.utcnow()})


def kubernetes_schedule_job_queue(
    job_ids,
    docker_image_path,
    volumes,
    gpus=0,
    environment_variables={},
    results_dir="/scratch/results",
    parallelism=10,
):
    """
    Example inputs:
    docker_image_path: ic-registry.epfl.ch/mlo/jobmonitor_worker
    volumes: ['pv-mlodata1'] or {'pv-mlodata1': '/mlodata1'}
    """
    if not isinstance(volumes, Iterable):
        raise ValueError(
            "Volumes should either be an iterable (list, tuple) or a dictionary {'volume_name': 'mount_path'}."
        )
    if not isinstance(volumes, dict):
        volumes = {volume: "/" + volume for volume in volumes}  # use volume-name as mount-path

    kubernetes.config.load_kube_config()
    client = kubernetes_client.BatchV1Api()
    random_id = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    job_name = "{}-queue-{}".format(os.getenv("USER"), random_id)
    metadata = k.V1ObjectMeta(name=job_name, labels=dict(app="jobmonitor", user=os.getenv("USER")))
    pod_spec = kubernetes_create_base_pod_spec(
        cmd=["jobrun", "--queue-mode", *job_ids],
        docker_image_path=docker_image_path,
        gpus=gpus,
        environment_variables={"JOBMONITOR_RESULTS_DIR": results_dir, **environment_variables},
        volumes=volumes,
    )
    job = k.V1Job(
        metadata=metadata,
        spec=k.V1JobSpec(
            backoff_limit=0,
            completions=len(job_ids),
            parallelism=parallelism,
            template=k.V1PodTemplateSpec(metadata=metadata, spec=pod_spec),
        ),
    )
    client.create_namespaced_job(c.KUBERNETES_NAMESPACE, job)


def kubernetes_create_base_pod_spec(
    cmd: List[str], docker_image_path, gpus=0, mem=128, cpu=20, environment_variables={}, volumes={}
):
    return k.V1PodSpec(
        host_ipc=True,  # Against shared memory limit
        restart_policy="Never",
        volumes=[
            k.V1Volume(
                name=volume,
                persistent_volume_claim=k.V1PersistentVolumeClaimVolumeSource(claim_name=volume),
            )
            for volume in volumes.keys()
        ],
        node_selector={"hardware-type": "CPUONLY"} if gpus == 0 else None,
        containers=[
            k.V1Container(
                name="worker",
                image=docker_image_path,
                env=[
                    k.V1EnvVar(name=name, value=value)
                    for name, value in environment_variables.items()
                ],
                volume_mounts=[
                    k.V1VolumeMount(mount_path=mount_path, name=volume)
                    for volume, mount_path in volumes.items()
                ],
                resources=(
                    k.V1ResourceRequirements(
                        limits={"nvidia.com/gpu": gpus, "memory": f"{mem}Gi", "cpu": cpu},
                        requests={"memory": f"{mem * 2 // 3}Gi", "cpu": cpu * 2 // 3},
                    )
                ),
                command=["/entrypoint.sh", *cmd],
            )
        ],
    )


def result_dir(job):
    root_dir = os.getenv("JOBMONITOR_RESULTS_DIR")
    if isinstance(job, str):
        job = job_by_id(job)
    return os.path.join(root_dir, job["output_dir"])


def describe_git_state(directory):
    try:
        repo = Repo(directory)
        is_dirty = repo.is_dirty()
        commit = repo.commit()
        author = commit.author
        return (
            repo.remotes.origin.url,
            author.name,
            author.email,
            commit.hexsha,
            commit.message,
            is_dirty,
        )
    except InvalidGitRepositoryError:
        return None, None, None, None, None, None
    except AttributeError:
        return None, None, None, None, None, None


def upload_code_package(directory=".", excludes=None):
    """
    Uploads a compressed tar file with directory contents to mongodb (gridfs).
    This package can be used in a job specification.
    :return (1) ObjectId of the inserted file, (2) list of included files
    """
    included_files = []

    # Make a function used to select/exclude files for the package
    if excludes is None:
        excludes = []

    def filter_fn(tarinfo):
        basename = os.path.basename(tarinfo.name)
        if any(fnmatch(basename, pattern) for pattern in excludes):
            return None
        included_files.append(tarinfo.name)
        return tarinfo

    # Collect some metadata about the directory and git
    directory_basename = os.path.basename(os.path.realpath(directory))
    remote, author_name, author_email, commit, commit_message, is_dirty = describe_git_state(
        directory
    )
    metadata = {
        "gitAuthorEmail": author_email,
        "gitAuthorName": author_name,
        "gitCommit": commit,
        "gitCommitMessage": commit_message,
        "gitRepository": remote,
        "gitWasDirty": is_dirty,
    }

    try:
        # Create a temporary tar file
        tmp = NamedTemporaryFile("rb")
        with tarfile.open(tmp.name, "w:gz") as fp:
            fp.add(directory, recursive=True, filter=filter_fn)
        # Upload it to MongoDB
        gridfs_id = c.gridfs.put(tmp, filename=directory_basename + ".tgz", metadata=metadata)
        return gridfs_id, included_files
    finally:
        tmp.close()


def download_code_package(package_id, destination):
    if type(package_id) == str:
        package_id = ObjectId(package_id)
    try:
        tmp = NamedTemporaryFile("wb+")
        with c.gridfs.get(package_id) as fp:
            tmp.write(fp.read())
        tmp.seek(0)
        with tarfile.open(tmp.name, "r") as fp:
            def is_within_directory(directory, target):
                
                abs_directory = os.path.abspath(directory)
                abs_target = os.path.abspath(target)
            
                prefix = os.path.commonprefix([abs_directory, abs_target])
                
                return prefix == abs_directory
            
            def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
            
                for member in tar.getmembers():
                    member_path = os.path.join(path, member.name)
                    if not is_within_directory(path, member_path):
                        raise Exception("Attempted Path Traversal in Tar File")
            
                tar.extractall(path, members, numeric_owner=numeric_owner) 
                
            
            safe_extract(fp, destination)
    finally:
        tmp.close()


InfluxSeries = namedtuple("InfluxEntry", ["measurement", "tags", "data"])


# def influx_query(query, merge=False):
#     """Get a list of timeseries as {measurement, tags, data} from InfluxDB"""
#     import pandas as pd

#     raw_data = c.influx.query(query)
#     series = []
#     for (measurement, tags), values in raw_data.items():
#         dataframe = pd.DataFrame(values)
#         dataframe["measurement"] = measurement
#         if tags is not None:
#             for key, value in tags.items():
#                 dataframe[key] = value
#         series.append(InfluxSeries(measurement=measurement, tags=tags, data=dataframe))
#         if "time" in dataframe:
#             dataframe.time = pd.to_datetime(dataframe.time)
#     if not merge:
#         return series
#     elif len(series) == 0:
#         return None
#     else:
#         return pd.concat([s.data for s in series])
