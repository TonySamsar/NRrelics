"""Manual inventory, editable requirements and optimal relic loadouts."""
import json
import copy
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QSpinBox, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget, QFrame, QScrollArea,
                               QSplitter)

from core.official_relics import load_official_relics
from core.relic_inventory import RelicInventory
from core.loadout_optimizer import load_catalog, optimize
from core.relic_scoring import POSITIVE_TIERS, normalize_affix_tiers
from core.utils import get_user_data_path, get_resource_path
from ui.dialogs.optimizer_constraint_dialog import ConstraintGroupDialog, describe_requirement


def _vocabulary(mode, negative=False):
    files = (['deepnight_neg.txt'] if negative else
             ['normal.txt', 'normal_special.txt'] if mode == 'normal' else ['deepnight_pos.txt'])
    names = set()
    for filename in files:
        with open(get_resource_path('data/' + filename), encoding='utf-8') as source:
            names.update(line.strip() for line in source if line.strip())
    return sorted(names)


class AffixPickerDialog(QDialog):
    def __init__(self, vocabulary, current='', parent=None):
        super().__init__(parent)
        self.setWindowTitle('查询并选择词条')
        self.resize(760, 580)
        layout = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText('输入词条关键词')
        layout.addWidget(self.search)
        self.items = QListWidget()
        layout.addWidget(self.items)
        layout.addWidget(QLabel('右键词条所在行可勾选或取消勾选。'))
        for name in vocabulary:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name == current else Qt.Unchecked)
            self.items.addItem(item)
        self.items.itemChanged.connect(self._single_check)
        self.search.textChanged.connect(self._filter)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _single_check(self, chosen):
        if chosen.checkState() == Qt.Checked:
            self.items.blockSignals(True)
            for index in range(self.items.count()):
                item = self.items.item(index)
                if item is not chosen:
                    item.setCheckState(Qt.Unchecked)
            self.items.blockSignals(False)

    def _filter(self, text):
        for index in range(self.items.count()):
            item = self.items.item(index)
            item.setHidden(text.strip().lower() not in item.text().lower())

    def _accept(self):
        if self.selected():
            self.accept()

    def selected(self):
        for index in range(self.items.count()):
            item = self.items.item(index)
            if item.checkState() == Qt.Checked:
                return item.text()
        return ''


class EditRelicDialog(QDialog):
    def __init__(self, parent=None, relic=None):
        super().__init__(parent)
        self.setWindowTitle('编辑遗物' if relic else '添加遗物')
        self.resize(900, 540)
        layout=QVBoxLayout(self)
        form=QFormLayout()
        self.name = relic.get('name','') if relic else ''
        self.kind=QComboBox(); self.kind.addItems(['普通遗物','深夜遗物'])
        self.kind.setCurrentIndex(1 if relic and relic['relic_type']=='deepnight' else 0)
        self.color=QComboBox(); self.color.addItems(['红','蓝','绿','黄'])
        if relic and relic['color'] in ('红','蓝','绿','黄'):
            self.color.setCurrentText(relic['color'])
        negative_vocab=set(_vocabulary('deepnight',negative=True))
        old_affixes=list(relic.get('affixes',[])) if relic else []
        positives=[affix for affix in old_affixes if affix not in negative_vocab]
        negatives=[affix for affix in old_affixes if affix in negative_vocab]
        self.slots=[{'positive': positives[i] if i<len(positives) else '',
                     'negative': negatives[i] if i<len(negatives) else ''}
                    for i in range(3)]
        self.affix_rows=[]
        form.addRow('类别',self.kind)
        form.addRow('颜色',self.color)
        for index in range(3):
            pair=QVBoxLayout()
            labels={}
            for negative in (False,True):
                row=QHBoxLayout()
                caption=QLabel('负面' if negative else '正面')
                label=QLabel()
                label.setWordWrap(True)
                button=QPushButton('查询词条…')
                button.clicked.connect(lambda checked=False, i=index, n=negative:self._pick_affix(i,n))
                clear=QPushButton('清除')
                clear.clicked.connect(lambda checked=False, i=index, n=negative:self._clear_affix(i,n))
                row.addWidget(caption);row.addWidget(label,1)
                row.addWidget(button);row.addWidget(clear)
                container=QWidget();container.setLayout(row)
                pair.addWidget(container)
                labels['negative' if negative else 'positive']=(label,container)
            form.addRow(f'第 {index+1} 组',pair)
            self.affix_rows.append(labels)
        layout.addLayout(form)
        self.kind.currentIndexChanged.connect(self._refresh_affixes)
        self._refresh_affixes()
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept);buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept(self):
        mode='deepnight' if self.kind.currentIndex() else 'normal'
        valid_positive=set(_vocabulary(mode))
        valid_negative=set(_vocabulary(mode,negative=True)) if mode=='deepnight' else set()
        positives=[slot['positive'] for slot in self.slots if slot['positive']]
        negatives=[slot['negative'] for slot in self.slots if slot['negative'] and mode=='deepnight']
        if not positives:
            QMessageBox.warning(self,'词条未完成','至少选择一条正面词条。');return
        if any(name not in valid_positive for name in positives):
            QMessageBox.warning(self,'正面词条无效','请从当前类别的正面词条库选择。');return
        if any(name not in valid_negative for name in negatives):
            QMessageBox.warning(self,'负面词条无效','请从深夜负面词条库选择。');return
        if mode=='deepnight' and any(slot['negative'] and not slot['positive'] for slot in self.slots):
            QMessageBox.warning(self,'词条未配对','每条负面词条必须与同组正面词条配对。');return
        if len(positives)!=len(set(positives)) or len(negatives)!=len(set(negatives)):
            QMessageBox.warning(self,'重复词条','同一遗物不能重复选择完全相同的词条。');return
        self.accept()

    def _refresh_affixes(self, *_):
        for index,labels in enumerate(self.affix_rows):
            for kind,(label,container) in labels.items():
                label.setText(self.slots[index][kind] or '未选择')
                container.setVisible(kind=='positive' or self.kind.currentIndex()==1)

    def _clear_affix(self,index,negative=False):
        self.slots[index]['negative' if negative else 'positive']=''
        self._refresh_affixes()

    def _pick_affix(self,index,negative=False):
        mode='deepnight' if self.kind.currentIndex() else 'normal'
        kind='negative' if negative else 'positive'
        picker=AffixPickerDialog(_vocabulary(mode,negative=negative),self.slots[index][kind],self)
        if picker.exec()==QDialog.Accepted:
            self.slots[index][kind]=picker.selected()
            self._refresh_affixes()

    def values(self):
        affixes=[]
        for slot in self.slots:
            if slot['positive']:
                affixes.append(slot['positive'])
                if self.kind.currentIndex() and slot['negative']:
                    affixes.append(slot['negative'])
        return ('deepnight' if self.kind.currentIndex() else 'normal',
                self.color.currentText(),affixes,self.name,None)


class SolveWorker(QThread):
    finished_result=Signal(object)
    failed=Signal(str)

    def __init__(self, args, kwargs):
        super().__init__()
        self.args=args; self.kwargs=kwargs

    def run(self):
        try:self.finished_result.emit(optimize(*self.args,**self.kwargs))
        except Exception as exc:self.failed.emit(str(exc))


class LoadoutResultDialog(QDialog):
    """Show each solution as relic cards in the vessel's left-to-right slot order."""

    def __init__(self, result, rows_by_id, parent=None):
        super().__init__(parent)
        self.setWindowTitle('遗物组合结果')
        self.resize(1540, 840)
        root=QVBoxLayout(self)
        title=QLabel(f"最优 {result['score']} 分｜备选须达到 {result['min_score']} 分"
                     f"（最优分的 {round(result['min_ratio']*100)}%）")
        title.setStyleSheet('font-size:18pt;font-weight:bold;')
        root.addWidget(title)
        note=QLabel('遗物按器皿槽位从左到右排列。每件下方显示它在整套组合中的实际计分；'
                    '0 分表示该词条在当前流派无效或已由另一件同名词条计分。')
        note.setWordWrap(True)
        root.addWidget(note)
        scroll=QScrollArea();scroll.setWidgetResizable(True)
        content=QWidget();content.setMinimumWidth(1370)
        vertical=QVBoxLayout(content);vertical.setSpacing(22)
        negatives=set(_vocabulary('deepnight',negative=True))
        if 'solutions' in result:
            start=result.get('page_offset',0)
            solutions=[(f"第 {start+index} 名" + ('｜已证明最优' if start+index==1 else ''),
                        solution)
                       for index,solution in enumerate(result['solutions'],1)]
        else:
            solutions=[('第一阶段｜最优组合',result)]
            solutions.extend((f'第二阶段｜达标组合 {index}',solution)
                             for index,solution in enumerate(result['alternatives'],1))
        for caption,solution in solutions:
            section=QFrame();section.setFrameShape(QFrame.StyledPanel)
            section_layout=QVBoxLayout(section)
            heading=QLabel(f"{caption}　{solution.get('vessel_name',solution['vessel'])}　"
                           f"总得分 {solution['score']} 分")
            heading.setStyleSheet('font-size:15pt;font-weight:bold;padding:6px;')
            section_layout.addWidget(heading)
            cards=QHBoxLayout();cards.setSpacing(10)
            for mode,label in (('normal','普通遗物'),('deepnight','深夜遗物')):
                mode_items=sorted((item for item in solution['relics']
                                   if item['relic_type']==mode),
                                  key=lambda item:item.get('slot_index',0))
                for relic in mode_items:
                    cards.addWidget(self._relic_card(relic,label,rows_by_id,negatives))
            cards.addStretch()
            section_layout.addLayout(cards)
            vertical.addWidget(section)
        vertical.addStretch()
        scroll.setWidget(content);root.addWidget(scroll)
        close=QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        root.addWidget(close)

    @staticmethod
    def _relic_card(relic, label, rows_by_id, negatives):
        card=QFrame();card.setFrameShape(QFrame.StyledPanel)
        card.setFixedWidth(215)
        layout=QVBoxLayout(card);layout.setSpacing(6)
        title=QLabel(f"{label}｜{relic['color']}色｜第 {relic.get('slot_index',0)+1} 槽")
        title.setStyleSheet('font-size:12pt;font-weight:bold;')
        layout.addWidget(title)
        row=rows_by_id.get(relic['id'])
        location=QLabel(f"遗物表格第 {row} 行" if row else '当前表格中未找到')
        location.setStyleSheet('font-size:11pt;color:#176b8b;')
        layout.addWidget(location)
        if relic.get('name'):
            name=QLabel(relic['name']);name.setWordWrap(True)
            name.setStyleSheet('font-size:11pt;font-weight:bold;')
            layout.addWidget(name)
        positive=[name for name in relic['affixes'] if name not in negatives]
        negative=[name for name in relic['affixes'] if name in negatives]
        for index in range(max(len(positive),len(negative))):
            for names,is_negative in ((positive,False),(negative,True)):
                if index>=len(names):continue
                affix=names[index]
                points=relic.get('affix_points',{}).get(affix,0)
                text=QLabel(f"{'负面' if is_negative else '正面'}｜{affix}　{points:+d} 分")
                text.setWordWrap(True)
                text.setStyleSheet('font-size:9pt;color:#a05050;' if is_negative
                                   else 'font-size:11pt;')
                layout.addWidget(text)
        adjustment=relic.get('official_adjustment',0)
        if adjustment:
            correction=QLabel(f'官方遗物整体评分修正　{adjustment:+d} 分')
            correction.setWordWrap(True)
            correction.setStyleSheet('font-size:10pt;color:#8b6b17;')
            layout.addWidget(correction)
        layout.addStretch()
        total=QLabel(f"本件贡献 {relic.get('score_contribution',0):+d} 分")
        total.setStyleSheet('font-size:12pt;font-weight:bold;')
        layout.addWidget(total)
        return card


class RelicPage(QWidget):
    def __init__(self, preset_manager):
        super().__init__()
        self.setObjectName('RelicPage')
        self.inventory=RelicInventory()
        self.presets=preset_manager
        self.builds,self.vessels=load_catalog()
        self.items=[]
        self.rules_path=Path(get_user_data_path('data/optimizer_rules.json'))
        try:self.rules=json.loads(self.rules_path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.rules={}
        if not isinstance(self.rules,dict):self.rules={}
        self.groups=[]
        layout=QVBoxLayout(self)
        layout.setContentsMargins(28,28,28,28)
        title=QLabel('遗物管理');title.setStyleSheet('font-size:24pt;font-weight:bold')
        layout.addWidget(title)
        self.splitter=QSplitter(Qt.Vertical)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(10)
        self.splitter.setStyleSheet('QSplitter::handle:vertical { background: #8f9ca9; }')
        inventory_panel=QWidget()
        inventory_layout=QVBoxLayout(inventory_panel)
        inventory_layout.setContentsMargins(0,0,0,0)
        optimizer_panel=QWidget()
        optimizer_layout=QVBoxLayout(optimizer_panel)
        optimizer_layout.setContentsMargins(0,0,0,0)
        inventory_panel.setMinimumHeight(170)
        optimizer_panel.setMinimumHeight(260)
        self.splitter.addWidget(inventory_panel)
        self.splitter.addWidget(optimizer_panel)
        self.splitter.setStretchFactor(0,3)
        self.splitter.setStretchFactor(1,2)
        layout.addWidget(self.splitter,1)
        controls=QHBoxLayout()
        for label,handler in (('添加遗物',self._add),('编辑勾选遗物',self._edit),
                              ('删除勾选遗物',self._delete),('全选',self._select_all),
                              ('全部取消',self._clear_all),('刷新库存',self.refresh)):
            button=QPushButton(label);button.clicked.connect(handler);controls.addWidget(button)
        inventory_layout.addLayout(controls)
        self.table=QTableWidget(0,4)
        self.table.setHorizontalHeaderLabels(['选择','类别','颜色','词条'])
        self.table.setColumnWidth(0,55)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        inventory_layout.addWidget(self.table,1)
        inventory_layout.addWidget(QLabel('官方遗物已全部内置并默认拥有；表格左侧为当前行号，官方遗物不可编辑或删除。'))
        inventory_layout.addWidget(QLabel('右键遗物所在行可勾选或取消勾选；灰色勾选框表示不可修改的官方遗物。'))

        select=QHBoxLayout()
        self.hero=QComboBox()
        for name in dict.fromkeys(build['hero'] for build in self.builds):
            self.hero.addItem(name,name)
        self.normal=QComboBox();self.deep=QComboBox()
        select.addWidget(QLabel('角色'));select.addWidget(self.hero)
        select.addWidget(QLabel('普通流派'));select.addWidget(self.normal,2)
        select.addWidget(QLabel('深夜流派'));select.addWidget(self.deep,2)
        self.show_other_builds=QCheckBox('显示其他角色流派')
        self.show_other_builds.setToolTip('允许普通、深夜预设选择其他角色的流派；高脚杯仍按左侧角色匹配。')
        select.addWidget(self.show_other_builds)
        optimizer_layout.addLayout(select)
        self.hero.currentIndexChanged.connect(self._refresh_preset_choices)
        self.show_other_builds.toggled.connect(self._refresh_preset_choices)
        self.normal.currentIndexChanged.connect(self._load_groups)
        self.deep.currentIndexChanged.connect(self._load_groups)

        threshold=QHBoxLayout()
        threshold.addWidget(QLabel('第二阶段最低分：最优分的'))
        self.ratio=QSpinBox();self.ratio.setRange(0,100);self.ratio.setValue(75)
        self.ratio.setSuffix('%')
        threshold.addWidget(self.ratio)
        threshold.addStretch()
        self.solve=QPushButton('计算最优与达标组合')
        self.solve.clicked.connect(self._solve)
        threshold.addWidget(self.solve)
        optimizer_layout.addLayout(threshold)

        optimizer_layout.addWidget(QLabel('本次优化的必备条件（各组都须满足；同组任选一项）'))
        self.group_list=QListWidget();self.group_list.setMinimumHeight(90)
        optimizer_layout.addWidget(self.group_list,1)
        optimizer_layout.addWidget(QLabel('右键条件组所在行可勾选或取消勾选。'))
        buttons=QHBoxLayout()
        for label,handler in (('添加条件组',self._add_group),('编辑选中组',self._edit_group),
                              ('移除选中组',self._remove_group),('查看完整逻辑',self._show_logic)):
            button=QPushButton(label);button.clicked.connect(handler);buttons.addWidget(button)
        optimizer_layout.addLayout(buttons)
        self.result_status=QLabel('计算完成后点击下方按钮查看遗物组合。')
        optimizer_layout.addWidget(self.result_status)
        self.view_result=QPushButton('查看遗物组合')
        self.view_result.setEnabled(False)
        self.view_result.clicked.connect(self._open_result)
        result_buttons=QHBoxLayout()
        result_buttons.addWidget(self.view_result)
        self.prev_results=QPushButton('查看上一页')
        self.prev_results.setEnabled(False)
        self.prev_results.clicked.connect(self._previous_page)
        result_buttons.addWidget(self.prev_results)
        self.next_results=QPushButton('继续寻找下 10 套')
        self.next_results.setEnabled(False)
        self.next_results.clicked.connect(self._next_page)
        result_buttons.addWidget(self.next_results)
        optimizer_layout.addLayout(result_buttons)
        self.latest_result=None
        self.search_snapshot=None
        self.seen_combinations=[]
        self.page_offset=0
        self.result_pages=[]
        self.current_page=-1
        self._refresh_preset_choices()
        self.refresh()

    def showEvent(self,event):
        super().showEvent(event)
        self._refresh_preset_choices()

    def refresh(self):
        self.items=self.inventory.list_owned()
        self.table.setRowCount(len(self.items))
        self.table.setVerticalHeaderLabels([str(index+1) for index in range(len(self.items))])
        for row,item in enumerate(self.items):
            official=item.get('official_id') is not None
            selector=QTableWidgetItem('')
            selector.setFlags(Qt.ItemIsEnabled if official else
                              Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            if official:
                selector.setFlags(Qt.ItemIsUserCheckable)
            selector.setCheckState(Qt.Unchecked)
            self.table.setItem(row,0,selector)
            for col,value in enumerate(('深夜' if item['relic_type']=='deepnight' else
                                        '官方普通' if official else '普通',
                                        item['color'],
                                        (item['name']+'｜' if official else '')+'；'.join(item['affixes'])),1):
                cell=QTableWidgetItem(str(value))
                cell.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.table.setItem(row,col,cell)

    def _save(self,item=None):
        dialog=EditRelicDialog(self,item)
        if dialog.exec()!=QDialog.Accepted:return
        try:self.inventory.save_manual(item['id'] if item else None,*dialog.values())
        except Exception as exc:
            QMessageBox.warning(self,'保存失败',str(exc));return
        self.refresh()

    def _add(self):self._save()

    def _edit(self):
        selected=self._checked_rows()
        if len(selected)!=1:
            QMessageBox.information(self,'编辑遗物','请只勾选一件遗物。');return
        if self.items[selected[0]].get('official_id') is not None:
            QMessageBox.information(self,'编辑遗物','内置官方遗物不可修改。');return
        self._save(self.items[selected[0]])

    def _checked_rows(self):
        return [row for row in range(self.table.rowCount())
                if self.items[row].get('official_id') is None
                and self.table.item(row,0).checkState()==Qt.Checked]

    def _select_all(self):
        for row in range(self.table.rowCount()):
            if self.items[row].get('official_id') is None:
                self.table.item(row,0).setCheckState(Qt.Checked)

    def _clear_all(self):
        for row in range(self.table.rowCount()):
            if self.items[row].get('official_id') is not None:continue
            self.table.item(row,0).setCheckState(Qt.Unchecked)

    def _delete(self):
        rows=self._checked_rows()
        if not rows:return
        if QMessageBox.question(self,'删除遗物',f'从可用库存中移除勾选的 {len(rows)} 件遗物？\n扫描记录仍会保留。',
                                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes:return
        self.inventory.delete_owned([self.items[row]['id'] for row in rows])
        self.refresh()

    def _refresh_preset_choices(self,*_):
        hero=self.hero.currentData()
        show_all=self.show_other_builds.isChecked()
        for combo,mode in ((self.normal,'normal'),(self.deep,'deepnight')):
            prior=combo.currentData()
            combo.blockSignals(True);combo.clear()
            combo.addItem('不优化此类遗物','none')
            combo.addItem('通用预设','general')
            presets=self.presets.get_dedicated_presets(mode)
            ordered=sorted(presets.items(),key=lambda item:
                           (not item[1].get('name','').startswith(hero+'｜'),
                            item[1].get('name','')))
            for pid,preset in ordered:
                if not preset.get('is_active',True):continue
                name=preset.get('name','')
                if show_all or name.startswith(hero+'｜') or '｜' not in name:
                    combo.addItem(name,pid)
            index=combo.findData(prior)
            if index<0:
                default=next((pid for pid,preset in self.presets.get_dedicated_presets(mode).items()
                              if preset.get('name','').startswith(hero+'｜')),None)
                index=combo.findData(default)
            combo.setCurrentIndex(max(index,0));combo.blockSignals(False)
        self._load_groups()

    def _rule_key(self):
        return '|'.join((str(self.hero.currentData()),str(self.normal.currentData()),
                         str(self.deep.currentData())))

    def _load_groups(self,*_):
        saved=self.rules.get(self._rule_key(),[])
        self.groups=[]
        if isinstance(saved,list):
            for group in saved:
                if not isinstance(group,list):continue
                unique={}
                for entry in group:
                    if not isinstance(entry,dict):continue
                    normalized={key:value for key,value in entry.items() if key!='mode'}
                    marker=(normalized.get('kind'),normalized.get('name'),normalized.get('id'))
                    unique[marker]=normalized
                if unique:self.groups.append(list(unique.values()))
        self._render_groups()

    def _render_groups(self):
        self.group_list.clear()
        for index,group in enumerate(self.groups,1):
            item=QListWidgetItem(f"条件 {index}："+' 或 '.join(describe_requirement(x) for x in group))
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.group_list.addItem(item)
        if not self.groups:self.group_list.addItem('无必备条件；仅按分数、颜色和槽位求解')

    def _save_groups(self):
        self.rules[self._rule_key()]=self.groups
        self.rules_path.write_text(json.dumps(self.rules,ensure_ascii=False,indent=2),encoding='utf-8')
        self._render_groups()

    def _effective_affixes(self,mode):
        general=self.presets.get_general_preset(mode) or {'affixes':[]}
        pid=(self.normal if mode=='normal' else self.deep).currentData()
        if pid == 'none':return []
        dedicated=(self.presets.get_dedicated_presets(mode).get(pid)
                   if pid!='general' else general)
        inherited,_=normalize_affix_tiers(general)
        local,_=normalize_affix_tiers(dedicated or {'affixes':[]})
        names=set(inherited)|set(local)
        names-=set((dedicated or {}).get('excluded_affixes',[]))
        if mode=='deepnight':
            for source in (general,dedicated or {}):
                names.update(name for name,tier in source.get('negative_tiers',{}).items()
                             if tier in POSITIVE_TIERS)
        return sorted(names)

    def _choices(self):
        names=set()
        for mode in ('normal','deepnight'):
            names.update(self._effective_affixes(mode))
        choices=[{'kind':'affix','name':name} for name in sorted(names)]
        if self.normal.currentData() != 'none':
            choices += [{'kind':'official','id':entry['id'],
                         'label':f"{entry['name']}（{entry['color']}）"}
                        for entry in load_official_relics()]
        return choices

    def _add_group(self):
        dialog=ConstraintGroupDialog(self._choices(),parent=self)
        if dialog.exec()==QDialog.Accepted:
            self.groups.append(dialog.selected());self._save_groups()

    def _edit_group(self):
        checked=self._checked_groups()
        if len(checked)!=1:
            QMessageBox.information(self,'编辑条件组','请只勾选一个条件组。');return
        index=checked[0]
        dialog=ConstraintGroupDialog(self._choices(),self.groups[index],self)
        if dialog.exec()==QDialog.Accepted:
            self.groups[index]=dialog.selected();self._save_groups()

    def _remove_group(self):
        for index in reversed(self._checked_groups()):
            del self.groups[index]
        self._save_groups()

    def _checked_groups(self):
        return [index for index in range(len(self.groups))
                if self.group_list.item(index).checkState()==Qt.Checked]

    def _show_logic(self):
        text=('没有必备条件。' if not self.groups else
              '\n并且\n'.join('（'+' 或 '.join(describe_requirement(x) for x in group)+'）'
                              for group in self.groups))
        QMessageBox.information(self,'当前必备条件逻辑',text)

    def _solve(self):
        if self.normal.currentData() == self.deep.currentData() == 'none':
            QMessageBox.information(self,'选择流派','请至少选择普通或深夜流派中的一种。')
            return
        build={'id':'manual','hero':self.hero.currentData()}
        path=get_user_data_path('data/settings.json')
        try:settings=json.loads(Path(path).read_text(encoding='utf-8'))
        except (OSError,ValueError):settings={}
        self.solve.setEnabled(False)
        self.view_result.setEnabled(False)
        self.next_results.setEnabled(False)
        self.latest_result=None
        self.page_offset=0
        self.seen_combinations=[]
        self.result_pages=[]
        self.current_page=-1
        self.prev_results.setEnabled(False)
        self.result_status.setText('正在证明最优分并寻找前 10 套…')
        args=(build,copy.deepcopy(self.items),self.vessels,copy.deepcopy(self.presets),
              settings.get('scoring',{}))
        kwargs={'normal_preset_id':self.normal.currentData(),
                'deepnight_preset_id':self.deep.currentData(),
                'required_groups':copy.deepcopy(self.groups),
                'min_ratio':self.ratio.value()/100,
                'page_size':10, 'time_limit':30}
        self.search_snapshot=(args,kwargs,
                              {item['id']:index for index,item in enumerate(self.items,1)})
        self.pending_next=False
        self.search_error=False
        self._start_worker(args,kwargs)

    def _start_worker(self,args,kwargs):
        self.worker=SolveWorker(args,kwargs)
        self.worker.finished_result.connect(self._show_result)
        self.worker.failed.connect(self._show_error)
        self.worker.finished.connect(self._search_finished)
        self.worker.start()

    def _next_page(self):
        if not self.search_snapshot or not self.latest_result:return
        if self.current_page + 1 < len(self.result_pages):
            self._display_page(self.current_page + 1)
            return
        args,base_kwargs,_=self.search_snapshot
        kwargs=dict(base_kwargs,excluded_combinations=self.seen_combinations,
                    anchor_score=self.result_pages[0]['score'])
        self.pending_next=True
        self.search_error=False
        self.solve.setEnabled(False)
        self.next_results.setEnabled(False)
        self.result_status.setText('正在证明下 10 套的排名…')
        self._start_worker(args,kwargs)

    def _previous_page(self):
        if self.current_page > 0:
            self._display_page(self.current_page - 1)

    def _display_page(self,index):
        self.current_page=index
        self.latest_result=self.result_pages[index]
        count=len(self.latest_result['solutions'])
        start=self.latest_result['page_offset']+1
        self.result_status.setText(
            f"已证明最优分 {self.latest_result['score']}；当前显示第 {start}–{start+count-1} 名。"
            + ('可继续查找。' if self.result_pages[-1]['has_more'] else '已找完达标方案。'))
        self.view_result.setEnabled(True)
        self.prev_results.setEnabled(index>0)
        self.next_results.setEnabled(index+1<len(self.result_pages) or
                                     self.result_pages[-1]['has_more'])

    def _search_finished(self):
        self.solve.setEnabled(True)
        if self.result_pages and not self.search_error:
            self._display_page(self.current_page)

    def _show_result(self,result):
        if result is None or not result.get('solutions'):
            self.result_status.setText('没有更多达标组合。' if self.pending_next else
                                       '没有可行组合。请检查库存、器皿颜色和必备条件。')
            if self.result_pages:
                self.result_pages[-1]['has_more']=False
            return
        if self.pending_next:
            result['page_offset']=self.page_offset
        else:
            result['page_offset']=0
        self.page_offset+=len(result['solutions'])
        self.seen_combinations=result['excluded_combinations']
        self.result_pages.append(result)
        self._display_page(len(self.result_pages)-1)

    def _open_result(self):
        if self.latest_result is None:return
        rows_by_id=self.search_snapshot[2]
        LoadoutResultDialog(self.latest_result,rows_by_id,self).exec()

    def _show_error(self,error):
        self.search_error=True
        self.result_status.setText('计算失败：'+error)
