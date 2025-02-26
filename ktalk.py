import sys
import os
import zipfile
import datetime
import time
import re
from PyQt6 import QtWidgets, QtCore, QtGui
import subprocess

# Windows 전용 레지스트리 모듈
if os.name == 'nt':
    import winreg

CURRENT_VERSION = "v1.0.0"  # 현재 버전

# --------------------------- 유틸리티 함수 ---------------------------
def format_size(size):
    """바이트 단위의 파일 크기를 사람이 읽기 쉬운 형식으로 변환."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"

def format_time(seconds):
    """초 단위의 시간을 hh:mm:ss 형식으로 변환."""
    return str(datetime.timedelta(seconds=int(seconds)))

# --------------------------- 검색 작업 Worker ---------------------------
class SearchWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int)  # (현재 진행, 총 파일 수)
    finished = QtCore.pyqtSignal(list)        # 각 파일에 대해 (매칭 여부, 매칭된 줄들의 리스트 [(줄번호, 해당줄 텍스트), ...])

    def __init__(self, files, search_term):
        super().__init__()
        self.files = files  # list of tuples (full_path, base_file_name)
        self.search_term = search_term.lower()
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        results = []
        total = len(self.files)
        for i, (full_path, file_name) in enumerate(self.files):
            if self._cancelled:
                break
            match = False
            line_matches = []
            # 파일명 검색
            if self.search_term in file_name.lower():
                match = True
            # 파일 내용 검색 (텍스트 파일로 가정)
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    for num, line in enumerate(f, 1):
                        if self._cancelled:
                            break
                        if self.search_term in line.lower():
                            line_matches.append((num, line.rstrip()))
                if line_matches:
                    match = True
            except Exception:
                pass
            results.append((match, line_matches))
            self.progress.emit(i + 1, total)
        self.finished.emit(results)

# --------------------------- 압축 작업 Worker (멀티스레딩) ---------------------------
class CompressionWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int)  # (현재 완료된 파일 수, 전체 파일 수)
    finished = QtCore.pyqtSignal(str)         # 압축 파일 경로 전달
    error = QtCore.pyqtSignal(str)

    def __init__(self, files_to_compress, zip_path):
        super().__init__()
        self.files_to_compress = files_to_compress  # list of tuples (full_path, arcname)
        self.zip_path = zip_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def read_file(self, full_path, arcname):
        if self._cancelled:
            raise Exception("압축 작업이 취소되었습니다.")
        try:
            with open(full_path, "rb") as f:
                data = f.read()
            return (arcname, data)
        except Exception as e:
            raise Exception(f"파일 읽기 실패: {full_path} - {str(e)}")

    def run(self):
        total = len(self.files_to_compress)
        results = []
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(self.read_file, full_path, arcname): (full_path, arcname) 
                           for full_path, arcname in self.files_to_compress}
                count = 0
                for future in as_completed(futures):
                    if self._cancelled:
                        raise Exception("압축 작업이 취소되었습니다.")
                    result = future.result()
                    results.append(result)
                    count += 1
                    self.progress.emit(count, total)
            if self._cancelled:
                raise Exception("압축 작업이 취소되었습니다.")
            with zipfile.ZipFile(self.zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for arcname, data in results:
                    if self._cancelled:
                        raise Exception("압축 작업이 취소되었습니다.")
                    zipf.writestr(arcname, data)
            self.finished.emit(self.zip_path)
        except Exception as e:
            self.error.emit(str(e))

# --------------------------- 파일 삭제 Worker ---------------------------
class DeletionWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int, float, float, float)
    finished = QtCore.pyqtSignal()
    error = QtCore.pyqtSignal(str)

    def __init__(self, files_to_delete):
        super().__init__()
        self.files_to_delete = files_to_delete  # list of full_path
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        total = len(self.files_to_delete)
        start_time = time.time()
        for i, file_path in enumerate(self.files_to_delete):
            if self._cancelled:
                break
            try:
                os.remove(file_path)
            except Exception as e:
                self.error.emit(f"파일 삭제 오류: {file_path} - {str(e)}")
                return
            deleted = i + 1
            elapsed = time.time() - start_time
            percent = (deleted / total) * 100
            avg = elapsed / deleted if deleted else 0
            remaining = avg * (total - deleted)
            self.progress.emit(deleted, total, percent, elapsed, remaining)
        self.finished.emit()

# --------------------------- 메인 윈도우 ---------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("카카오톡 파일 확인기")
        self.resize(1000, 800)
        self.setWindowIcon(QtGui.QIcon("./icon.png"))  # 프로그램 아이콘 적용

        self.file_items = []  # 각 항목: (QTreeWidgetItem, full_path, base_file_name)
        self.active_worker = None  # 현재 진행중인 작업의 worker 참조

        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QVBoxLayout(central_widget)

        # 상단 툴바: GitHub, 업데이트 확인 (텍스트만 사용)
        toolbar = QtWidgets.QToolBar()
        self.addToolBar(QtCore.Qt.ToolBarArea.TopToolBarArea, toolbar)
        spacer = QtWidgets.QWidget()
        spacer.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        github_action = QtGui.QAction("GitHub", self)
        github_action.triggered.connect(self.open_github)
        toolbar.addAction(github_action)
        update_action = QtGui.QAction("업데이트 확인", self)
        update_action.triggered.connect(self.check_update)
        toolbar.addAction(update_action)

        # 검색 바: 검색 입력, 검색 버튼, 검색 초기화
        search_layout = QtWidgets.QHBoxLayout()
        self.search_input = QtWidgets.QLineEdit()
        self.search_input.setPlaceholderText("파일명 또는 파일 내용 검색")
        self.search_input.returnPressed.connect(self.start_search)
        self.search_button = QtWidgets.QPushButton("검색")
        self.search_button.clicked.connect(self.start_search)
        self.search_reset_button = QtWidgets.QPushButton("검색 초기화")
        self.search_reset_button.clicked.connect(self.reset_search)
        search_layout.addWidget(self.search_input)
        search_layout.addWidget(self.search_button)
        search_layout.addWidget(self.search_reset_button)
        main_layout.addLayout(search_layout)

        # 진행바 및 상태 레이블 (검색/압축/삭제 진행 상황 표시)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_label = QtWidgets.QLabel()
        self.progress_label.setVisible(False)
        main_layout.addWidget(self.progress_bar)
        main_layout.addWidget(self.progress_label)

        # 취소 버튼 (작업 진행 시 보임)
        self.cancel_button = QtWidgets.QPushButton("취소")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self.cancel_operation)
        main_layout.addWidget(self.cancel_button)

        # 파일 목록 QTreeWidget: 파일명, 확장자, 용량, 수정 날짜
        self.tree = QtWidgets.QTreeWidget()
        self.tree.setHeaderLabels(["파일명", "확장자", "용량", "수정 날짜"])
        self.tree.setSortingEnabled(True)
        self.tree.setColumnWidth(0, 400)  # 파일명 열 넓게
        self.tree.itemDoubleClicked.connect(self.open_file)
        main_layout.addWidget(self.tree)

        # 하단 버튼 영역: 전체 선택, 전체 삭제, 선택 삭제, 열기, 압축하기, 새로고침
        button_layout = QtWidgets.QHBoxLayout()
        self.select_all_checkbox = QtWidgets.QCheckBox("전체 선택")
        self.select_all_checkbox.stateChanged.connect(self.toggle_select_all)
        self.btn_clear_all = QtWidgets.QPushButton("전체 삭제")
        self.btn_clear_selected = QtWidgets.QPushButton("선택 삭제")
        self.btn_open = QtWidgets.QPushButton("열기")
        self.btn_compress = QtWidgets.QPushButton("압축하기")
        self.btn_refresh = QtWidgets.QPushButton("새로고침")
        button_layout.addWidget(self.select_all_checkbox)
        button_layout.addWidget(self.btn_clear_all)
        button_layout.addWidget(self.btn_clear_selected)
        button_layout.addWidget(self.btn_open)
        button_layout.addWidget(self.btn_compress)
        button_layout.addWidget(self.btn_refresh)
        button_layout.addStretch()
        main_layout.addLayout(button_layout)

        # 버튼 연결
        self.btn_clear_all.clicked.connect(self.start_delete_all)
        self.btn_clear_selected.clicked.connect(self.start_delete_selected)
        self.btn_open.clicked.connect(self.open_selected_files)
        self.btn_compress.clicked.connect(self.compress_files)
        self.btn_refresh.clicked.connect(self.check_folder_and_list_files)

        # 카카오톡 설치 및 파일 목록 로딩
        self.check_kakaotalk_installation()
        self.check_folder_and_list_files()

    # --------------------------- 상단 액션 ---------------------------
    def open_github(self):
        import webbrowser
        webbrowser.open("https://github.com/gaon12/talkcleaner")

    def check_update(self):
        """GitHub API를 통해 최신 릴리즈 버전을 확인하여 업데이트가 있으면 해당 페이지로 이동."""
        import urllib.request, json, webbrowser
        api_url = "https://api.github.com/repos/gaon12/talkcleaner/releases/latest"
        try:
            with urllib.request.urlopen(api_url, timeout=5) as response:
                data = json.loads(response.read().decode())
            latest_version = data.get("tag_name", "")
            if latest_version and latest_version != CURRENT_VERSION:
                reply = QtWidgets.QMessageBox.question(
                    self, "업데이트 확인",
                    f"최신 버전은 {latest_version}입니다. 업데이트 페이지로 이동할까요?",
                    QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
                )
                if reply == QtWidgets.QMessageBox.StandardButton.Yes:
                    webbrowser.open(data.get("html_url", ""))
            else:
                QtWidgets.QMessageBox.information(self, "업데이트 확인", "현재 최신 버전입니다.")
        except Exception:
            QtWidgets.QMessageBox.warning(self, "업데이트 확인", "업데이트 확인을 할 수 없습니다. 인터넷 연결을 확인해 주세요!")

    def check_kakaotalk_installation(self):
        """카카오톡 설치 여부를 exe 경로와 레지스트리로 확인."""
        possible_paths = [
            r"C:\Program Files\Kakao\KakaoTalk\KakaoTalk.exe",
            r"C:\Program Files (x86)\Kakao\KakaoTalk\KakaoTalk.exe"
        ]
        installed_exe = any(os.path.exists(path) for path in possible_paths)
        installed_reg = False
        if os.name == 'nt':
            reg_paths = [r"SOFTWARE\Kakao\KakaoTalk", r"SOFTWARE\WOW6432Node\Kakao\KakaoTalk"]
            for reg_path in reg_paths:
                try:
                    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                    winreg.CloseKey(key)
                    installed_reg = True
                    break
                except FileNotFoundError:
                    continue
        if not (installed_exe or installed_reg):
            QtWidgets.QMessageBox.warning(
                self, "카카오톡 미설치", "카카오톡이 설치되어 있지 않습니다.",
                QtWidgets.QMessageBox.StandardButton.Ok
            )

    def check_folder_and_list_files(self):
        """'카카오톡 받은 파일' 폴더의 파일 목록(파일명, 확장자, 용량, 수정 날짜)을 로딩."""
        self.tree.clear()
        self.file_items.clear()
        folder_path = os.path.join(os.environ['USERPROFILE'], 'Documents', '카카오톡 받은 파일')
        if not os.path.isdir(folder_path):
            QtWidgets.QMessageBox.warning(
                self, "폴더 미존재", f"폴더가 존재하지 않습니다:\n{folder_path}",
                QtWidgets.QMessageBox.StandardButton.Ok
            )
            return
        for root, dirs, files in os.walk(folder_path):
            relative_path = os.path.relpath(root, folder_path)
            for file in files:
                display_text = file if relative_path == '.' else os.path.join(relative_path, file)
                full_path = os.path.join(root, file)
                _, ext = os.path.splitext(file)
                try:
                    size = os.path.getsize(full_path)
                    size_str = format_size(size)
                except Exception:
                    size_str = "N/A"
                try:
                    mod_time = os.path.getmtime(full_path)
                    mod_time_str = datetime.datetime.fromtimestamp(mod_time).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    mod_time_str = "N/A"
                item = QtWidgets.QTreeWidgetItem(self.tree)
                item.setText(0, display_text)
                item.setText(1, ext)
                item.setText(2, size_str)
                item.setText(3, mod_time_str)
                # 체크박스 추가
                item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, QtCore.Qt.CheckState.Unchecked)
                self.file_items.append((item, full_path, os.path.basename(file)))
        self.tree.expandAll()

    # --------------------------- 파일 열기 기능 ---------------------------
    def open_selected_files(self):
        """체크된 파일 열기."""
        for item, full_path, base_name in self.file_items:
            if item.checkState(0) == QtCore.Qt.CheckState.Checked:
                self.open_path(full_path)

    def open_file(self, item, column):
        """파일 항목 더블클릭 시 열기.
           자식 항목(검색 결과 줄)인 경우, 저장된 (파일경로, 줄번호) 정보를 확인하여
           Notepad++가 설치되어 있으면 해당 줄로 이동하여 열고, 그렇지 않으면 기본 연결 프로그램으로 파일만 엽니다."""
        data = item.data(0, QtCore.Qt.ItemDataRole.UserRole)
        if data is not None:
            # 자식 항목인 경우 (검색 결과 줄)
            self.open_file_at_line(data[0], data[1])
        else:
            # 상위 항목이면 해당 파일 열기
            for it, full_path, base_name in self.file_items:
                if it == item:
                    self.open_path(full_path)
                    break

    def open_file_at_line(self, path, line):
        """
        Notepad++가 설치되어 있으면 해당 줄 번호로 열도록 시도.
        (Notepad++가 기본 연결 프로그램이 아닐 경우에도, 특정 줄 이동을 지원하므로)
        만약 Notepad++가 없으면 기본 연결 프로그램으로 파일만 열게 됩니다.
        """
        if os.name == 'nt':
            notepadpp = r"C:\Program Files\Notepad++\notepad++.exe"
            if os.path.exists(notepadpp):
                try:
                    subprocess.Popen([notepadpp, f"-n{line}", path])
                except Exception as e:
                    QtWidgets.QMessageBox.warning(self, "파일 열기", f"Notepad++로 열지 못했습니다:\n{str(e)}\n기본 프로그램으로 열겠습니다.")
                    os.startfile(path)
            else:
                os.startfile(path)
        else:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))

    def open_path(self, path):
        """운영체제 기본 프로그램으로 파일 열기."""
        try:
            if os.name == 'nt':
                os.startfile(path)
            else:
                QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path))
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "파일 열기", f"파일을 열 수 없습니다:\n{str(e)}")

    # --------------------------- 전체 선택 체크박스 ---------------------------
    def toggle_select_all(self, state):
        check_state = QtCore.Qt.CheckState.Checked if state == QtCore.Qt.CheckState.Checked else QtCore.Qt.CheckState.Unchecked
        for item, full_path, base_name in self.file_items:
            item.setCheckState(0, check_state)

    # --------------------------- 검색 기능 ---------------------------
    def start_search(self):
        """검색어를 이용해 파일명 및 파일 내용 검색 실행."""
        search_term = self.search_input.text().strip()
        if search_term == "":
            self.reset_search()
            return
        files_for_search = [(full_path, base_name) for (item, full_path, base_name) in self.file_items]
        self.search_button.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.cancel_button.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(files_for_search))
        self.progress_label.setText("검색 진행중...")
        self.search_thread = QtCore.QThread()
        self.search_worker = SearchWorker(files_for_search, search_term)
        self.active_worker = self.search_worker  # 현재 작업 설정
        self.search_worker.moveToThread(self.search_thread)
        self.search_worker.progress.connect(self.update_progress)
        self.search_worker.finished.connect(self.search_finished)
        self.search_thread.started.connect(self.search_worker.run)
        self.search_thread.start()

    def update_progress(self, value, total):
        self.progress_bar.setValue(value)

    def search_finished(self, results):
        max_length = 35  # 최대 표시할 글자 수 제한
        pattern = re.compile(re.escape(self.search_input.text().strip()), re.IGNORECASE)
        for i, (item, full_path, base_name) in enumerate(self.file_items):
            match, line_matches = results[i] if i < len(results) else (False, [])
            item.takeChildren()  # 기존 자식 제거
            item.setHidden(not match)
            if match and line_matches:
                for ln, line_text in line_matches:
                    if len(line_text) > max_length:
                        line_text = line_text[:max_length] + "..."
                    highlighted = pattern.sub(lambda m: f"<b>{m.group()}</b>", line_text)
                    child = QtWidgets.QTreeWidgetItem(item)
                    child.setText(0, f"라인 {ln}: {highlighted}")
                    child.setData(0, QtCore.Qt.ItemDataRole.UserRole, (full_path, ln))
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.search_button.setEnabled(True)
        self.search_thread.quit()
        self.search_thread.wait()
        self.search_worker.deleteLater()
        self.search_thread.deleteLater()
        self.active_worker = None

    def reset_search(self):
        """검색어 초기화 및 모든 파일 항목 표시, 자식 항목 제거."""
        self.search_input.clear()
        for item, full_path, base_name in self.file_items:
            item.setHidden(False)
            item.takeChildren()

    # --------------------------- 압축 기능 ---------------------------
    def compress_files(self):
        """체크된 파일이 있으면 해당 파일 압축, 없으면 전체 파일 압축 여부 확인 후 진행."""
        checked_files = [(full_path, item.text(0)) for item, full_path, base_name in self.file_items
                         if item.checkState(0) == QtCore.Qt.CheckState.Checked and not item.isHidden()]
        if not checked_files:
            reply = QtWidgets.QMessageBox.question(
                self, "압축하기", "체크된 파일이 없습니다. 전체 파일을 압축하시겠습니까?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
            )
            if reply == QtWidgets.QMessageBox.StandardButton.Yes:
                files_to_compress = [(full_path, item.text(0)) for item, full_path, base_name in self.file_items if not item.isHidden()]
            else:
                return
        else:
            files_to_compress = checked_files
        if not files_to_compress:
            QtWidgets.QMessageBox.information(self, "압축하기", "압축할 파일이 없습니다.")
            return
        zip_path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "압축 파일 저장",
            os.path.join(os.environ['USERPROFILE'], 'Desktop', f"kakaotalk_file_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"),
            "Zip Files (*.zip)"
        )
        if not zip_path:
            return
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.cancel_button.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(files_to_compress))
        self.progress_label.setText("압축 진행중...")
        self.btn_compress.setEnabled(False)
        self.comp_thread = QtCore.QThread()
        self.comp_worker = CompressionWorker(files_to_compress, zip_path)
        self.active_worker = self.comp_worker  # 현재 작업 설정
        self.comp_worker.moveToThread(self.comp_thread)
        self.comp_worker.progress.connect(self.update_progress)
        self.comp_worker.finished.connect(self.compression_finished)
        self.comp_worker.error.connect(self.compression_error)
        self.comp_thread.started.connect(self.comp_worker.run)
        self.comp_thread.start()

    def compression_finished(self, zip_path):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.btn_compress.setEnabled(True)
        self.comp_thread.quit()
        self.comp_thread.wait()
        self.comp_worker.deleteLater()
        self.comp_thread.deleteLater()
        self.active_worker = None
        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("압축 완료")
        msg_box.setText(f"압축이 완료되었습니다:\n{zip_path}")
        open_button = msg_box.addButton("열기", QtWidgets.QMessageBox.ButtonRole.AcceptRole)
        close_button = msg_box.addButton("닫기", QtWidgets.QMessageBox.ButtonRole.RejectRole)
        msg_box.exec()
        if msg_box.clickedButton() == open_button:
            self.open_path(zip_path)

    def compression_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.btn_compress.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "압축하기", f"압축 중 오류 발생:\n{err_msg}")
        self.comp_thread.quit()
        self.comp_thread.wait()
        self.comp_worker.deleteLater()
        self.comp_thread.deleteLater()
        self.active_worker = None

    # --------------------------- 파일 삭제 기능 ---------------------------
    def start_delete_all(self):
        """전체 파일 삭제 전 경고 후 실제 삭제 진행."""
        files_to_delete = [full_path for item, full_path, base_name in self.file_items if not item.isHidden()]
        if not files_to_delete:
            QtWidgets.QMessageBox.information(self, "삭제", "삭제할 파일이 없습니다.")
            return
        reply = QtWidgets.QMessageBox.question(
            self, "전체 삭제", "정말로 전체 파일을 삭제하시겠습니까?\n(이 작업은 복구할 수 없습니다.)",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.start_deletion(files_to_delete)

    def start_delete_selected(self):
        """선택된 파일 삭제 전 경고 후 실제 삭제 진행."""
        files_to_delete = [full_path for item, full_path, base_name in self.file_items if item.checkState(0) == QtCore.Qt.CheckState.Checked]
        if not files_to_delete:
            QtWidgets.QMessageBox.information(self, "삭제", "선택된 파일이 없습니다.")
            return
        reply = QtWidgets.QMessageBox.question(
            self, "선택 삭제", "정말로 선택된 파일을 삭제하시겠습니까?\n(이 작업은 복구할 수 없습니다.)",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        if reply != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.start_deletion(files_to_delete)

    def start_deletion(self, files_to_delete):
        """DeletionWorker를 사용하여 파일 삭제 진행 및 진행률, 예상 시간 등을 표시."""
        self.progress_bar.setVisible(True)
        self.progress_label.setVisible(True)
        self.cancel_button.setVisible(True)
        self.progress_bar.setValue(0)
        self.progress_bar.setMaximum(len(files_to_delete))
        self.progress_label.setText("삭제 진행중...")
        self.btn_clear_all.setEnabled(False)
        self.btn_clear_selected.setEnabled(False)
        self.del_thread = QtCore.QThread()
        self.del_worker = DeletionWorker(files_to_delete)
        self.active_worker = self.del_worker  # 현재 작업 설정
        self.del_worker.moveToThread(self.del_thread)
        self.del_worker.progress.connect(self.update_deletion_progress)
        self.del_worker.finished.connect(self.deletion_finished)
        self.del_worker.error.connect(self.deletion_error)
        self.del_thread.started.connect(self.del_worker.run)
        self.del_thread.start()

    def update_deletion_progress(self, deleted, total, percent, elapsed, remaining):
        self.progress_bar.setValue(deleted)
        self.progress_label.setText(f"삭제: {deleted}/{total}  {percent:.1f}%\n경과: {format_time(elapsed)} / 예상남은: {format_time(remaining)}")

    def deletion_finished(self):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.btn_clear_all.setEnabled(True)
        self.btn_clear_selected.setEnabled(True)
        self.del_thread.quit()
        self.del_thread.wait()
        self.del_worker.deleteLater()
        self.del_thread.deleteLater()
        self.active_worker = None
        QtWidgets.QMessageBox.information(self, "삭제 완료", "파일 삭제가 완료되었습니다.")
        self.check_folder_and_list_files()

    def deletion_error(self, err_msg):
        self.progress_bar.setVisible(False)
        self.progress_label.setVisible(False)
        self.cancel_button.setVisible(False)
        self.btn_clear_all.setEnabled(True)
        self.btn_clear_selected.setEnabled(True)
        QtWidgets.QMessageBox.warning(self, "삭제 오류", err_msg)
        self.del_thread.quit()
        self.del_thread.wait()
        self.del_worker.deleteLater()
        self.del_thread.deleteLater()
        self.active_worker = None

    # --------------------------- 취소 버튼 기능 ---------------------------
    def cancel_operation(self):
        """현재 진행중인 작업 취소."""
        if self.active_worker is not None:
            self.active_worker.cancel()
            QtWidgets.QMessageBox.information(self, "취소", "작업이 취소되었습니다.")
            self.cancel_button.setVisible(False)

    # --------------------------- main() ---------------------------
    def main(self):
        from PyQt6.QtGui import QGuiApplication
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QtWidgets.QApplication(sys.argv)
        app.setStyle("Fusion")
        window = MainWindow()
        window.show()
        sys.exit(app.exec())

def main():
    from PyQt6.QtGui import QGuiApplication
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
