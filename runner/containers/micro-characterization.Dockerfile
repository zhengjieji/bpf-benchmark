# syntax=docker/dockerfile:1.6
# Four pure-compute paths only. The existing runner-runtime image serves other suites.
FROM docker.io/library/ubuntu:24.04 AS micro-characterization
ARG IMAGE_WORKSPACE
ARG RUN_TARGET_ARCH
ARG RUNNER_BUILD_DIR_NAME
ARG KERNEL_IMAGE_NAME
ARG SIM_PROOF_DIR_NAME
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash binutils ca-certificates kmod libelf1t64 libfmt9 libspdlog1.12 \
        libssl3t64 libyaml-cpp0.8 llvm-18 python3 python3-yaml util-linux zlib1g \
    && rm -rf /var/lib/apt/lists/*
WORKDIR ${IMAGE_WORKSPACE}

COPY --link --from=micro-host-kernel-image /${KERNEL_IMAGE_NAME} /artifacts/kernel/${KERNEL_IMAGE_NAME}
COPY --link --from=micro-host-kernel-config /config /artifacts/kernel/config
COPY --link --from=micro-host-kernel-config /manifest.json /artifacts/manifest.json
COPY --link --from=micro-host-kernel-modules / /artifacts/lib/modules/
COPY --link --from=micro-host-kop / /artifacts/kop/
RUN kernel_release="$(python3 -c 'import json; print(json.load(open("/artifacts/manifest.json"))["kernel_release"])')" \
    && install -d /artifacts/boot /boot \
    && cp /artifacts/kernel/config "/artifacts/boot/config-${kernel_release}" \
    && cp /artifacts/kernel/config "/boot/config-${kernel_release}" \
    && depmod -b /artifacts "$kernel_release"

COPY --link --from=micro-host-runner /micro_exec ./runner/${RUNNER_BUILD_DIR_NAME}/micro_exec
COPY --link --from=micro-host-runner /native_loader/libnative_loader.so /usr/local/lib/bpfrejit/libnative_loader.so
COPY --link --from=micro-host-programs / /artifacts/user/micro-programs/${RUN_TARGET_ARCH}/
COPY --link --from=micro-host-native-proofs / /artifacts/user/micro-programs/${RUN_TARGET_ARCH}/
COPY --link --from=micro-host-programs /kernel_offsets.h /artifacts/kernel/kernel_offsets.h
COPY --link --from=micro-host-proofs / /artifacts/user/stage2-programs/${RUN_TARGET_ARCH}/${SIM_PROOF_DIR_NAME}/
COPY --link --chmod=0755 --from=micro-host-native-link /native-link /usr/local/bin/native-link
COPY --link --chmod=0755 --from=micro-host-bpftool /bpftool /usr/local/bin/bpftool
RUN install -d /opt && ln -sfn /artifacts/user /opt/bpf-benchmark && ldconfig

COPY runner/__init__.py ./runner/
COPY runner/config ./runner/config
COPY runner/libs ./runner/libs
COPY runner/suites ./runner/suites
COPY micro/*.py ./micro/
COPY micro/config ./micro/config
COPY --link --from=micro-source /*.bpf.c ./micro/programs/
COPY --link --from=micro-source /*.h ./micro/programs/
COPY --link --from=micro-host-kernel-config /source-manifest.json /artifacts/source-manifest.json
COPY --link --from=micro-host-kernel-config /host-packages.tsv /artifacts/host-packages.tsv
COPY --link --from=micro-host-kernel-config /target-packages.tsv /artifacts/target-packages.tsv
RUN install -d micro/results micro/generated-inputs /var/tmp/bpfrejit-runtime

ENV BPFREJIT_IMAGE_WORKSPACE=${IMAGE_WORKSPACE} \
    BPFREJIT_MICRO_IMAGE_PROFILE=characterization \
    BPFREJIT_NATIVE_LOADER_SO=/usr/local/lib/bpfrejit/libnative_loader.so \
    BPFREJIT_NATIVE_LOADER_REQUIRE_PREBUILT_PROOF=1 \
    PYTHONPATH=${IMAGE_WORKSPACE} \
    RUN_TARGET_ARCH=${RUN_TARGET_ARCH}
