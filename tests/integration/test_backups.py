#!/usr/bin/env python3
# Copyright 2023 Canonical Ltd.
# See LICENSE file for licensing details.
import logging
import uuid
from typing import Dict, Tuple

import boto3
import pytest as pytest
from pytest_operator.plugin import OpsTest
from tenacity import Retrying, stop_after_attempt, wait_exponential

from . import architecture
from .helpers import (
    CHARM_BASE,
    DATABASE_APP_NAME,
    MOVE_RESTORED_CLUSTER_TO_ANOTHER_BUCKET,
    backup_operations,
    construct_endpoint,
    db_connect,
    get_password,
    get_primary,
    get_unit_address,
    switchover,
    wait_for_idle_on_blocked,
)
from .juju_ import juju_major_version

ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE = "the S3 repository has backups from another cluster"
FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE = (
    "failed to access/create the bucket, check your S3 settings"
)
S3_INTEGRATOR_APP_NAME = "s3-integrator"
if juju_major_version < 3:
    tls_certificates_app_name = "tls-certificates-operator"
    if architecture.architecture == "arm64":
        tls_channel = "legacy/edge"
    else:
        tls_channel = "legacy/stable"
    tls_config = {"generate-self-signed-certificates": "true", "ca-common-name": "Test CA"}
else:
    tls_certificates_app_name = "self-signed-certificates"
    if architecture.architecture == "arm64":
        tls_channel = "latest/edge"
    else:
        tls_channel = "latest/stable"
    tls_config = {"ca-common-name": "Test CA"}

logger = logging.getLogger(__name__)

AWS = "AWS"
GCP = "GCP"


@pytest.fixture(scope="module")
async def cloud_configs(github_secrets) -> None:
    # Define some configurations and credentials.
    configs = {
        AWS: {
            "endpoint": "https://s3.amazonaws.com",
            "bucket": "data-charms-testing",
            "path": f"/postgresql-vm/{uuid.uuid1()}",
            "region": "us-east-1",
        },
        GCP: {
            "endpoint": "https://storage.googleapis.com",
            "bucket": "data-charms-testing",
            "path": f"/postgresql-vm/{uuid.uuid1()}",
            "region": "",
        },
    }
    credentials = {
        AWS: {
            "access-key": github_secrets["AWS_ACCESS_KEY"],
            "secret-key": github_secrets["AWS_SECRET_KEY"],
        },
        GCP: {
            "access-key": github_secrets["GCP_ACCESS_KEY"],
            "secret-key": github_secrets["GCP_SECRET_KEY"],
        },
    }
    yield configs, credentials
    # Delete the previously created objects.
    logger.info("deleting the previously created backups")
    for cloud, config in configs.items():
        session = boto3.session.Session(
            aws_access_key_id=credentials[cloud]["access-key"],
            aws_secret_access_key=credentials[cloud]["secret-key"],
            region_name=config["region"],
        )
        s3 = session.resource(
            "s3", endpoint_url=construct_endpoint(config["endpoint"], config["region"])
        )
        bucket = s3.Bucket(config["bucket"])
        # GCS doesn't support batch delete operation, so delete the objects one by one.
        for bucket_object in bucket.objects.filter(Prefix=config["path"].lstrip("/")):
            bucket_object.delete()


@pytest.mark.group(1)
@pytest.mark.abort_on_fail
async def test_backup_aws(ops_test: OpsTest, cloud_configs: Tuple[Dict, Dict], charm) -> None:
    """Build and deploy two units of PostgreSQL in AWS, test backup and restore actions."""
    config = cloud_configs[0][AWS]
    credentials = cloud_configs[1][AWS]

    await backup_operations(
        ops_test,
        S3_INTEGRATOR_APP_NAME,
        tls_certificates_app_name,
        tls_config,
        tls_channel,
        credentials,
        AWS,
        config,
        charm,
    )
    database_app_name = f"{DATABASE_APP_NAME}-aws"

    # Remove the relation to the TLS certificates operator.
    await ops_test.model.applications[database_app_name].remove_relation(
        f"{database_app_name}:certificates", f"{tls_certificates_app_name}:certificates"
    )

    new_unit_name = f"{database_app_name}/2"

    async with ops_test.fast_forward():
        # Scale up to be able to test primary and leader being different.
        await ops_test.model.applications[database_app_name].add_units(1)
        # Ensure that new unit become in blocked status, but is fully functional.
        await ops_test.model.block_until(
            lambda: ops_test.model.units.get(new_unit_name).workload_status_message
            == MOVE_RESTORED_CLUSTER_TO_ANOTHER_BUCKET,
            timeout=1000,
        )

    # Ensure replication is working correctly.
    address = get_unit_address(ops_test, new_unit_name)
    password = await get_password(ops_test, new_unit_name)
    patroni_password = await get_password(ops_test, new_unit_name, "patroni")
    with db_connect(host=address, password=password) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'backup_table_1');"
        )
        assert cursor.fetchone()[
            0
        ], f"replication isn't working correctly: table 'backup_table_1' doesn't exist in {new_unit_name}"
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'backup_table_2');"
        )
        assert not cursor.fetchone()[
            0
        ], f"replication isn't working correctly: table 'backup_table_2' exists in {new_unit_name}"
    connection.close()

    old_primary = await get_primary(ops_test, new_unit_name)
    switchover(ops_test, old_primary, patroni_password, new_unit_name)

    # Get the new primary unit.
    primary = await get_primary(ops_test, new_unit_name)
    # Check that the primary changed.
    for attempt in Retrying(
        stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30)
    ):
        with attempt:
            assert primary == new_unit_name

    # Ensure stanza is working correctly.
    logger.info("listing the available backups")
    action = await ops_test.model.units.get(new_unit_name).run_action("list-backups")
    await action.wait()
    backups = action.results.get("backups")
    assert backups, "backups not outputted"

    # Remove S3 relation to ensure "move to another cluster" blocked status is gone
    await ops_test.model.applications[database_app_name].remove_relation(
        f"{database_app_name}:s3-parameters", f"{S3_INTEGRATOR_APP_NAME}:s3-credentials"
    )

    await ops_test.model.wait_for_idle(status="active", timeout=1000)

    # Remove the database app.
    await ops_test.model.remove_application(database_app_name, block_until_done=True)

    # Remove the TLS operator.
    await ops_test.model.remove_application(tls_certificates_app_name, block_until_done=True)


@pytest.mark.group(2)
@pytest.mark.abort_on_fail
async def test_backup_gcp(ops_test: OpsTest, cloud_configs: Tuple[Dict, Dict], charm) -> None:
    """Build and deploy two units of PostgreSQL in GCP, test backup and restore actions."""
    config = cloud_configs[0][GCP]
    credentials = cloud_configs[1][GCP]

    await backup_operations(
        ops_test,
        S3_INTEGRATOR_APP_NAME,
        tls_certificates_app_name,
        tls_config,
        tls_channel,
        credentials,
        GCP,
        config,
        charm,
    )
    database_app_name = f"{DATABASE_APP_NAME}-gcp"

    # Remove the database app.
    await ops_test.model.remove_application(database_app_name, block_until_done=True)

    # Remove the TLS operator.
    await ops_test.model.remove_application(tls_certificates_app_name, block_until_done=True)


@pytest.mark.group(2)
async def test_restore_on_new_cluster(ops_test: OpsTest, github_secrets, charm) -> None:
    """Test that is possible to restore a backup to another PostgreSQL cluster."""
    previous_database_app_name = f"{DATABASE_APP_NAME}-gcp"
    database_app_name = f"new-{DATABASE_APP_NAME}"
    await ops_test.model.deploy(
        charm, application_name=previous_database_app_name, base=CHARM_BASE
    )
    await ops_test.model.deploy(
        charm,
        application_name=database_app_name,
        base=CHARM_BASE,
    )
    await ops_test.model.relate(previous_database_app_name, S3_INTEGRATOR_APP_NAME)
    await ops_test.model.relate(database_app_name, S3_INTEGRATOR_APP_NAME)
    async with ops_test.fast_forward():
        logger.info(
            "waiting for the database charm to become blocked due to existing backups from another cluster in the repository"
        )
        await wait_for_idle_on_blocked(
            ops_test,
            previous_database_app_name,
            2,
            S3_INTEGRATOR_APP_NAME,
            ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE,
        )
        logger.info(
            "waiting for the database charm to become blocked due to existing backups from another cluster in the repository"
        )
        await wait_for_idle_on_blocked(
            ops_test,
            database_app_name,
            0,
            S3_INTEGRATOR_APP_NAME,
            ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE,
        )

    # Remove the database app with the same name as the previous one (that was used only to test
    # that the cluster becomes blocked).
    await ops_test.model.remove_application(previous_database_app_name, block_until_done=True)

    # Run the "list backups" action.
    unit_name = f"{database_app_name}/0"
    logger.info("listing the available backups")
    action = await ops_test.model.units.get(unit_name).run_action("list-backups")
    await action.wait()
    backups = action.results.get("backups")
    assert backups, "backups not outputted"
    await wait_for_idle_on_blocked(
        ops_test,
        database_app_name,
        0,
        S3_INTEGRATOR_APP_NAME,
        ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE,
    )

    # Run the "restore backup" action.
    for attempt in Retrying(
        stop=stop_after_attempt(10), wait=wait_exponential(multiplier=1, min=2, max=30)
    ):
        with attempt:
            logger.info("restoring the backup")
            most_recent_backup = backups.split("\n")[-1]
            backup_id = most_recent_backup.split()[0]
            action = await ops_test.model.units.get(unit_name).run_action(
                "restore", **{"backup-id": backup_id}
            )
            await action.wait()
            restore_status = action.results.get("restore-status")
            assert restore_status, "restore hasn't succeeded"

    # Wait for the restore to complete.
    async with ops_test.fast_forward():
        unit = ops_test.model.units.get(f"{database_app_name}/0")
        await ops_test.model.block_until(
            lambda: unit.workload_status_message == MOVE_RESTORED_CLUSTER_TO_ANOTHER_BUCKET
        )

    # Check that the backup was correctly restored by having only the first created table.
    logger.info("checking that the backup was correctly restored")
    password = await get_password(ops_test, unit_name)
    address = get_unit_address(ops_test, unit_name)
    with db_connect(host=address, password=password) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name = 'backup_table_1');"
        )
        assert cursor.fetchone()[
            0
        ], "backup wasn't correctly restored: table 'backup_table_1' doesn't exist"
    connection.close()


@pytest.mark.group(2)
async def test_invalid_config_and_recovery_after_fixing_it(
    ops_test: OpsTest, cloud_configs: Tuple[Dict, Dict]
) -> None:
    """Test that the charm can handle invalid and valid backup configurations."""
    database_app_name = f"new-{DATABASE_APP_NAME}"

    # Provide invalid backup configurations.
    logger.info("configuring S3 integrator for an invalid cloud")
    await ops_test.model.applications[S3_INTEGRATOR_APP_NAME].set_config({
        "endpoint": "endpoint",
        "bucket": "bucket",
        "path": "path",
        "region": "region",
    })
    action = await ops_test.model.units.get(f"{S3_INTEGRATOR_APP_NAME}/0").run_action(
        "sync-s3-credentials",
        **{
            "access-key": "access-key",
            "secret-key": "secret-key",
        },
    )
    await action.wait()
    logger.info("waiting for the database charm to become blocked")
    unit = ops_test.model.units.get(f"{database_app_name}/0")
    await ops_test.model.block_until(
        lambda: unit.workload_status_message == FAILED_TO_ACCESS_CREATE_BUCKET_ERROR_MESSAGE
    )

    # Provide valid backup configurations, but from another cluster repository.
    logger.info(
        "configuring S3 integrator for a valid cloud, but with the path of another cluster repository"
    )
    await ops_test.model.applications[S3_INTEGRATOR_APP_NAME].set_config(cloud_configs[0][GCP])
    action = await ops_test.model.units.get(f"{S3_INTEGRATOR_APP_NAME}/0").run_action(
        "sync-s3-credentials",
        **cloud_configs[1][GCP],
    )
    await action.wait()
    logger.info("waiting for the database charm to become blocked")
    unit = ops_test.model.units.get(f"{database_app_name}/0")
    await ops_test.model.block_until(
        lambda: unit.workload_status_message == ANOTHER_CLUSTER_REPOSITORY_ERROR_MESSAGE
    )

    # Provide valid backup configurations, with another path in the S3 bucket.
    logger.info("configuring S3 integrator for a valid cloud")
    config = cloud_configs[0][GCP].copy()
    config["path"] = f"/postgresql/{uuid.uuid1()}"
    await ops_test.model.applications[S3_INTEGRATOR_APP_NAME].set_config(config)
    logger.info("waiting for the database charm to become active")
    await ops_test.model.wait_for_idle(
        apps=[database_app_name, S3_INTEGRATOR_APP_NAME], status="active"
    )
