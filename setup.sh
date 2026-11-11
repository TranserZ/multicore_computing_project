#!/usr/bin/env bash
# =============================================================================
# 공통 환경 설치 스크립트
#
# tools/ 아래에 gem5, McPAT, HotSpot, PARSEC 리소스를 같은 버전으로 설치한다.
# 이미 설치된 항목은 건너뛰므로 여러 번 실행해도 된다.
#
# 사용법:
#   ./setup.sh            전체 설치
#   JOBS=4 ./setup.sh     gem5 빌드 병렬도 지정 (기본: 메모리 2GB당 1개, 최대 코어 수)
#
# 필요 조건: Linux, Docker, KVM, git, curl, 디스크 여유 약 60GB
# =============================================================================
set -euo pipefail

# ---- 버전 고정 (팀 전체가 같은 버전을 쓰기 위함) -----------------------------
GEM5_TAG="v25.1.0.1"
DOCKER_IMAGE="ghcr.io/gem5/ubuntu-24.04_all-dependencies:v25-1"
MCPAT_REPO="https://github.com/HewlettPackard/mcpat.git"
MCPAT_COMMIT="74d4759"
HOTSPOT_REPO="https://github.com/uvahotspot/HotSpot.git"
HOTSPOT_COMMIT="f18831e"
KERNEL_URL="https://dist.gem5.org/dist/develop/kernels/x86/static/vmlinux-4.19.83"
KERNEL_MD5="28228eac2eea81ee3ce36305b7641c18"
DISK_URL="https://dist.gem5.org/dist/develop/images/x86/ubuntu-18-04/parsec.img.gz"
DISK_MD5="19131e62c5b10f137948ec5975391477"   # 압축 해제 후 parsec.img 기준

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS="$PROJ_DIR/tools"
PATCH_DIR="$PROJ_DIR/problem1/changes"

step() { echo; echo "==== $* ===="; }
ok()   { echo "  [OK] $*"; }
skip() { echo "  [SKIP] $*"; }
die()  { echo "  [ERROR] $*" >&2; exit 1; }

# Docker 컨테이너 안에서 명령 실행 (현재 사용자 권한으로)
in_docker() {
    local dir="$1"; shift
    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp \
        -v "$dir:/work" -w /work "$DOCKER_IMAGE" "$@"
}

# =============================================================================
step "0. 필요 조건 확인"
[[ "$(uname -s)" == "Linux" ]] || die "Linux에서만 동작합니다 (KVM 필요)."
for c in docker git curl md5sum gunzip; do
    command -v "$c" >/dev/null || die "'$c' 명령이 없습니다. 먼저 설치하세요."
done
docker info >/dev/null 2>&1 || die "Docker를 사용할 수 없습니다. Docker 데몬 실행 여부와 docker 그룹 포함 여부를 확인하세요 (sudo usermod -aG docker \$USER 후 재로그인)."
ok "Docker"
[[ -e /dev/kvm ]] || die "/dev/kvm이 없습니다. BIOS에서 가상화(VT-x/AMD-V)를 켜세요."
[[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm 접근 권한이 없습니다 (sudo usermod -aG kvm \$USER 후 재로그인)."
ok "KVM"
mkdir -p "$TOOLS/resources"
avail_gb=$(( $(df -Pk "$TOOLS" | awk 'NR==2{print $4}') / 1024 / 1024 ))
if [[ ! -f "$TOOLS/resources/parsec.img" && $avail_gb -lt 60 ]]; then
    die "디스크 여유 공간이 ${avail_gb}GB입니다. 약 60GB가 필요합니다."
fi
ok "디스크 여유 ${avail_gb}GB"

if [[ -z "${JOBS:-}" ]]; then
    mem_gb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
    JOBS=$(( mem_gb / 2 )); (( JOBS < 1 )) && JOBS=1
    (( JOBS > $(nproc) )) && JOBS=$(nproc)
fi

# =============================================================================
step "1. Docker 빌드 환경 ($DOCKER_IMAGE)"
if docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
    skip "이미 있음"
else
    docker pull "$DOCKER_IMAGE"
    ok "다운로드 완료"
fi

# =============================================================================
step "2. gem5 $GEM5_TAG"
if [[ ! -d "$TOOLS/gem5" ]]; then
    git clone --depth 1 --branch "$GEM5_TAG" https://github.com/gem5/gem5.git "$TOOLS/gem5"
fi
if [[ -x "$TOOLS/gem5/build/X86/gem5.opt" ]]; then
    skip "build/X86/gem5.opt 이미 있음"
else
    echo "  빌드 시작 (-j$JOBS). 30분~1시간 이상 걸립니다. 로그: tools/gem5_build.log"
    yes "" | in_docker "$TOOLS/gem5" scons build/X86/gem5.opt -j"$JOBS" \
        > "$TOOLS/gem5_build.log" 2>&1 \
        || die "gem5 빌드 실패. tools/gem5_build.log를 확인하세요."
    ok "빌드 완료"
fi

# =============================================================================
step "3. McPAT ($MCPAT_COMMIT + 버그 수정 패치)"
if [[ ! -d "$TOOLS/mcpat" ]]; then
    git clone "$MCPAT_REPO" "$TOOLS/mcpat"
    git -C "$TOOLS/mcpat" checkout -q "$MCPAT_COMMIT"
fi
for p in "$PATCH_DIR"/mcpat_*.patch; do
    if git -C "$TOOLS/mcpat" apply --reverse --check "$p" 2>/dev/null; then
        skip "패치 이미 적용됨: $(basename "$p")"
    else
        git -C "$TOOLS/mcpat" apply "$p" || die "패치 적용 실패: $p"
        rm -f "$TOOLS/mcpat/mcpat"   # 패치 후 다시 빌드
        ok "패치 적용: $(basename "$p")"
    fi
done
if [[ -x "$TOOLS/mcpat/mcpat" ]]; then
    skip "mcpat 이미 빌드됨"
else
    # Makefile이 -m32를 강제하므로 64비트 컴파일러를 직접 지정한다.
    in_docker "$TOOLS/mcpat" make -j"$(nproc)" opt CXX=g++ CC=gcc > "$TOOLS/mcpat_build.log" 2>&1 \
        || die "McPAT 빌드 실패. tools/mcpat_build.log를 확인하세요."
    ok "빌드 완료"
fi

# =============================================================================
step "4. HotSpot ($HOTSPOT_COMMIT)"
if [[ ! -d "$TOOLS/hotspot" ]]; then
    git clone "$HOTSPOT_REPO" "$TOOLS/hotspot"
    git -C "$TOOLS/hotspot" checkout -q "$HOTSPOT_COMMIT"
fi
if [[ -x "$TOOLS/hotspot/hotspot" ]]; then
    skip "hotspot 이미 빌드됨"
else
    in_docker "$TOOLS/hotspot" make > "$TOOLS/hotspot_build.log" 2>&1 \
        || die "HotSpot 빌드 실패. tools/hotspot_build.log를 확인하세요."
    ok "빌드 완료"
fi

# =============================================================================
step "5. PARSEC 리소스 (커널, 디스크 이미지)"
cd "$TOOLS/resources"
if [[ -f vmlinux-4.19.83 ]] && echo "$KERNEL_MD5  vmlinux-4.19.83" | md5sum -c --quiet 2>/dev/null; then
    skip "커널 이미 있음"
else
    curl -fL -o vmlinux-4.19.83 "$KERNEL_URL"
    echo "$KERNEL_MD5  vmlinux-4.19.83" | md5sum -c --quiet || die "커널 md5 불일치"
    ok "커널 다운로드 완료"
fi
if [[ -f parsec.img ]]; then
    skip "parsec.img 이미 있음 (md5 검사는 오래 걸려 생략. 필요하면: md5sum tools/resources/parsec.img)"
else
    echo "  디스크 이미지 다운로드 중 (압축 약 8GB, 해제 후 26GB)..."
    curl -fL -o parsec.img.gz "$DISK_URL"
    gunzip -f parsec.img.gz
    echo "$DISK_MD5  parsec.img" | md5sum -c --quiet || die "디스크 이미지 md5 불일치"
    ok "디스크 이미지 준비 완료"
fi

# =============================================================================
step "6. 설치 확인"
[[ -x "$TOOLS/gem5/build/X86/gem5.opt" ]] && ok "gem5:    tools/gem5/build/X86/gem5.opt"
[[ -x "$TOOLS/mcpat/mcpat" ]]             && ok "McPAT:   tools/mcpat/mcpat"
[[ -x "$TOOLS/hotspot/hotspot" ]]         && ok "HotSpot: tools/hotspot/hotspot"
[[ -f "$TOOLS/resources/parsec.img" ]]    && ok "PARSEC:  tools/resources/parsec.img, vmlinux-4.19.83"
echo
echo "설치 완료."
